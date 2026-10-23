#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""帧顺序不变量守护测试：属性同步帧必须排在应答帧之前。

★ 不变量（R1）：
    客户端 core/client/gateclientrpchandler.lua:entityMessageResponse
    一收到应答帧就 SafeCallback(callback, retStatus, response) 同步执行回调。
    因此「应答回调里需要读到的新属性值」的同步帧必须排在该应答帧之前。

★ 唯一入口（R3）：game_server/rpc_common.py::respond_after_sync

★ 实证事故（2026-10-23「铁窗泪」）：
    RequestEnterStreetSurface_CS 旧写法 return [应答] + syncs，
    客户端在回调里读 street_info.surface_enter_time 做昼夜一致性判定，
    读到旧值 -> error("服务器和客户端数据不同步") -> 昼夜不切、
    对话队列中断、任务停摆、剧情重播。

用法: python tests/test_frame_order.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_frame_order_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def frame_types(frames):
    return [int.from_bytes(bytes(f)[4:6], "little") for f in frames]


def run():
    uid = "frameorder"
    msmod._live_init_dict(uid)
    # _use_item 需要背包里有该道具，否则会早退（只回应答），用例就测不到顺序。
    _st = pstate.get_state(uid=uid)
    _st.props["item_bag"] = {
        "__tp__": "ItemBag", "__id__": 1324,
        "1": {"__id__": 1357, "id": "1", "itemId": 101, "num": 1000,
              "ext": {"__id__": 1358}},
    }
    _st.save()

    print("\n[1] 已迁移 handler：同步帧必须先于应答帧")
    # 每条 = (RPC 名, 参数, 是否必须产生同步帧)
    # 参数取自各 handler 的真实解析路径（探针实测），保证不会早退。
    cases = [
        ("SetCharacterList_CS", [1, [9001, 9002, 0]], True),
        ("SetCharacterListName_CS", [1, "\u9635\u5217"], True),
        ("SetPortrait_CS", [1], True),
        ("SetCanScore_CS", [], True),
        ("SetSignature_CS", ["\u7b7e\u540d"], True),
        ("RequestFetchTask_CS", [1], True),
        ("RequestDoneTask_CS", [1, 1], True),
        ("RequestDoneSomeTypeTask_CS", [1], True),
        ("updateStreetTraceTask_CS", [1000], True),
        ("RequestDispatchChar_CS", [5001, 9001], True),
        ("RequestDoneTitleTask_CS", [1], False),
        ("RequestRefreshTitleDailyTask_CS", [], False),
        ("Street_Mainline_Levelup_CS", [], False),
        ("UpgradeCharacter_CS", [9001, {}], True),
        ("UseItem_CS", [101, 100, None], True),
        ("RpcLeaveSpace", [], True),
    ]
    covered = 0
    for i, (rpc, params, must_sync) in enumerate(cases):
        idx = erpc.RPC_INDEX.get(rpc)
        if idx is None:
            check("%s 已注册" % rpc, False, "RPC 索引缺失")
            continue
        frames = guarded_handle(idx, msmod.mp_arr(params), 100 + i, uid=uid)
        ts = frame_types(frames)
        has_sync = msmod.PROPERTY_SYNC_TYPE in ts
        has_resp = 2 in ts
        if has_sync:
            covered += 1
            ok = has_resp and ts.index(msmod.PROPERTY_SYNC_TYPE) < ts.index(2)
            check("★ %s 同步帧先于应答帧" % rpc, ok, ts)
        else:
            # 该分支没改数据（早退或无需同步）：只要求「不该有同步帧却排在应答后」
            check("%s 无同步帧时只回应答" % rpc, must_sync is False and ts == [2],
                  (must_sync, ts))

    print("\n[2] 测试有效性：必须真的覆盖到同步帧（防恒真）")
    check("★ 至少 12 条用例真的产生了同步帧", covered >= 12, covered)

    print("\n[3] 昼夜切换（实证事故点）也必须是同步在前")
    _fr = guarded_handle(erpc.RPC_INDEX["RequestEnterStreetSurface_CS"],
                         msmod.mp_arr([1]), 200, uid=uid)
    _ts = frame_types(_fr)
    check("★ RequestEnterStreetSurface_CS 同步帧先于应答帧",
          msmod.PROPERTY_SYNC_TYPE in _ts and 2 in _ts
          and _ts.index(msmod.PROPERTY_SYNC_TYPE) < _ts.index(2), _ts)

    print("\n[4] 单向消息不受影响（返回空列表）")
    check("rid=0 的单向消息仍是 []",
          guarded_handle(msmod.RPC_INDEX_SET_SIGNATURE_CS, msmod.mp_arr(["x"]), 0) == [])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
