#!/usr/bin/env python
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


def entity_method(frame):
    """type=2 帧的 methodName：应答是 __rpc_response__，s2c 推送是自己的方法名。"""
    b = bytes(frame)
    if int.from_bytes(b[4:6], "little") != 2:
        return None
    fields = msmod.pb_parse(b[6:])
    nm = fields.get(2, [b""])[0]
    return nm.decode("utf-8", "replace") if isinstance(nm, (bytes, bytearray)) else nm

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
    # ★ 升级语义修复后：用例存档要有完整 item_bag + 材料（UpgradeCharacter 用例）
    from game_server import player_state as _psF
    _stF = _psF.get_state(uid=uid)
    _ibF2 = _stF.props.setdefault("item_bag", {})
    _ibF2["989"] = {"__id__": 990009, "id": "989", "itemId": 201, "num": 2}
    _ibF2["988"] = {"__id__": 990008, "id": "988", "itemId": 103, "num": 100000}
    _stF.set("item_bag", _ibF2)
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
        ("UpgradeCharacter_CS", [9001, {"201": 2}], True),
        ("UseItem_CS", [101, 100, None], True),
        ("RpcLeaveSpace", [], True),
        ("BattleTeach_Activate_CS", [101], True),
        ("Storyline_FinishGame", [1, True], True),
    ]
    covered = 0
    for i, (rpc, params, must_sync) in enumerate(cases):
        idx = erpc.RPC_INDEX.get(rpc)
        if idx is None:
            check(f"{rpc} 已注册", False, "RPC 索引缺失")
            continue
        frames = guarded_handle(idx, msmod.mp_arr(params), 100 + i, uid=uid)
        ts = frame_types(frames)
        has_sync = msmod.PROPERTY_SYNC_TYPE in ts
        has_resp = 2 in ts
        if has_sync:
            covered += 1
            ok = has_resp and ts.index(msmod.PROPERTY_SYNC_TYPE) < ts.index(2)
            check(f"★ {rpc} 同步帧先于应答帧", ok, ts)
        else:
            # 该分支没改数据（早退或无需同步）：只要求「不该有同步帧却排在应答后」
            check(f"{rpc} 无同步帧时只回应答", must_sync is False and ts == [2],
                  (must_sync, ts))

    print("\n[2] 测试有效性：必须真的覆盖到同步帧（防恒真）")
    check("★ 至少 14 条用例真的产生了同步帧", covered >= 14, covered)

    print("\n[3] 昼夜切换（实证事故点）也必须是同步在前")
    _fr = guarded_handle(erpc.RPC_INDEX["RequestEnterStreetSurface_CS"],
                         msmod.mp_arr([1]), 200, uid=uid)
    _ts = frame_types(_fr)
    check("★ RequestEnterStreetSurface_CS 同步帧先于应答帧",
          msmod.PROPERTY_SYNC_TYPE in _ts and 2 in _ts
          and _ts.index(msmod.PROPERTY_SYNC_TYPE) < _ts.index(2), _ts)

    print("\n[3b] 副本结算：同步在前、应答居中、SpaceFinish_SC 推送在应答之后")
    # 客户端实测顺序 sendFinishInfo -> sendFinishInfoCallback(应答) -> OnReceiveEndMsg(推送)，
    # 所以推送必须走 respond_after_sync 的 tail，不能塞进 syncs。
    _fin = {uid: {"win": True, "battle_items": {"201": 7},
                  "useTime": 10, "collectCount": 0}}
    _ffr = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                          msmod.mp_arr([_fin]), 201, uid=uid)
    _fmethods = [entity_method(f) for f in _ffr]
    _ftypes = frame_types(_ffr)
    _i_ack = _fmethods.index("__rpc_response__") if "__rpc_response__" in _fmethods else -1
    _i_push = (_fmethods.index("AvatarSpace_SpaceFinish_SC")
               if "AvatarSpace_SpaceFinish_SC" in _fmethods else -1)
    check("★ 结算含应答帧", _i_ack >= 0, _fmethods)
    check("★ 结算含 SpaceFinish_SC 推送", _i_push >= 0, _fmethods)
    check("★ 结算的属性同步全部排在应答之前",
          _i_ack >= 0 and all(i < _i_ack for i, t in enumerate(_ftypes)
                              if t == msmod.PROPERTY_SYNC_TYPE), _ftypes)
    check("★ SpaceFinish_SC 推送排在应答之后",
          _i_ack >= 0 and _i_push > _i_ack, _fmethods)

    print("\n[4] 单向消息不受影响（返回空列表）")
    check("rid=0 的单向消息仍是 []",
          guarded_handle(msmod.RPC_INDEX_SET_SIGNATURE_CS, msmod.mp_arr(["x"]), 0) == [])

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
