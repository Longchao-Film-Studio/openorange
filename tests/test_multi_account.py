#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多账号 / ownerid 帧路由（P0 遗留第 4 条）。

遗留原状：「多账号 / ownerid 的帧路由未建模（当前 30 处 _sync_frame 全传空
ownerid，不是当前 bug）」。

★ 2026-10-22 审计后的第二版：第一版的「帧不交叉」是**假阳性** ——
  它只收集了 A 的 item_bag 却收集了 B 的 item_bag+characterBag，
  而且跑的 RequestSwitchStreet_CS 根本不产生背包帧，
  所以 leaked 必然为空、无论路由对不对。
  现在改成：让 B 真的分配一个 A 没有的新对象 __id__，再让 A 跑一条
  会产生背包帧的 RPC（副本结算发奖），双向断言不交叉。

覆盖：
  1. 状态隔离：A 的 RPC 不改 B 的状态
  2. 帧隔离：A 的帧里有 A 自己的对象 id（非空断言），且没有 B 独有的 id
  3. ownerid 语义：默认空串（自己）；显式传 uid 写进帧；int/bytes 直接抛
  4. 影子缓存按客户端语义处理非空 ownerid（未知 owner 整帧丢弃）
  5. transport/accounts 关联表：bind / uid_of / release / claim 顺序 / 从 auth 还原

用法: python tests/test_multi_account.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_multi_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod, player_state as pstate, story  # noqa: E402
from game_server import rpc_common as rpc_common  # noqa: E402
from game_server.transport import accounts  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def state(uid):
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


def collect_ids(obj, out):
    """递归收集一个值里所有 __id__。"""
    if isinstance(obj, dict):
        v = obj.get("__id__")
        if isinstance(v, int):
            out.add(v)
        for k, vv in obj.items():
            if k in ("__id__", "__tp__"):
                continue
            collect_ids(vv, out)
    elif isinstance(obj, (list, tuple)):
        for vv in obj:
            collect_ids(vv, out)


def props_ids(props):
    """一个账号全部对象的 __id__ 集合。"""
    out = set()
    for k, v in props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            continue
        collect_ids(v, out)
    return out


def frame_info(frame):
    """解一条属性同步帧 -> (ownerid, pid, optype, name, ids_in_value)。"""
    fields = msmod.pb_parse(bytes(frame[6:]))
    owner_raw = fields.get(1, [b""])[0]
    ownerid = (owner_raw.decode("utf-8", "replace")
               if isinstance(owner_raw, (bytes, bytearray)) else owner_raw)
    pid = fields.get(2, [None])[0]
    optype = fields.get(3, [None])[0]
    name_raw = fields.get(4, [b""])[0]
    name = (name_raw.decode("utf-8", "replace")
            if isinstance(name_raw, (bytes, bytearray)) else name_raw)
    val_raw = fields.get(5, [None])[0]
    value = None
    if val_raw is not None:
        value = msmod.mp_decode(val_raw)[0]
    ids = set()
    if value is not None:
        collect_ids(value, ids)
    return ownerid, pid, optype, name, ids


FINISH_INFO = lambda uid: {  # noqa: E731
    "toStreetId": 101, "toStreetPosId": 0, "finishReason": 0,
    uid: {"win": True, "useTime": 60, "collectCount": 0,
          "battle_items": {"201": 5}},
}


def run():
    print("\n[1] 状态隔离：A 的 RPC 不改 B 的状态")
    a = state("multi_a")
    b = state("multi_b")
    b_street_before = b.props.get("street_id")
    b_bag_before = sorted((k, v.get("num")) for k, v in
                          (b.props.get("item_bag") or {}).items()
                          if isinstance(k, str) and not k.startswith("__"))
    guarded_handle(erpc.RPC_INDEX["RequestSwitchStreet_CS"],
                   msmod.mp_arr([1011, None]), 1, uid="multi_a")
    check("A 切到 1011", a.props.get("street_id") == 1011, a.props.get("street_id"))
    check("B 的 street_id 没变", b.props.get("street_id") == b_street_before,
          (b_street_before, b.props.get("street_id")))
    b_bag_after = sorted((k, v.get("num")) for k, v in
                         (b.props.get("item_bag") or {}).items()
                         if isinstance(k, str) and not k.startswith("__"))
    check("B 的背包没变", b_bag_before == b_bag_after,
          (len(b_bag_before), len(b_bag_after)))

    print("\n[2] 帧隔离：A 的帧里有 A 自己的对象 id，且没有 B 独有的 id")
    # ★ 关键：让 B 真的分配一个 A 没有的新对象（新背包格子），
    #   否则「B 独有 id 集合」是空的，断言就变成假阳性。
    a_ids = props_ids(a.props)
    # ★ 两个账号都是从同一份 initDict 建出来的，id 空间起点相同 ——
    #   直接 add_item 很可能拿到 A 也有的 id。所以先把 B 的分配器推高。
    b.props["_oo_next_id"] = max(a_ids) + 1000
    got_b = story.add_item(b.props, 77777, 1)
    check("B 成功新建一个背包格子", bool(got_b) and got_b[2] is True, got_b)
    b_new_id = b.props["item_bag"][got_b[1]]["__id__"]
    check("这个新 __id__ 确实只有 B 有", b_new_id not in a_ids, b_new_id)

    # A 跑一条会产生背包属性帧的 RPC：副本结算发奖
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                 "modeId": 1, "storyID": 1}]), 2,
                   uid="multi_a")
    frames_a = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                              msmod.mp_arr([FINISH_INFO("multi_a")]), 3,
                              uid="multi_a")
    frame_ids_a = set()
    frame_owners_a = set()
    bag_frame_count = 0
    a_bag_pid = a.props["item_bag"]["__id__"]
    for f in frames_a:
        if not isinstance(f, (bytes, bytearray)):
            continue
        try:
            owner, pid, _op, _n, ids = frame_info(f)
        except Exception:
            continue
        frame_ids_a |= ids
        frame_owners_a.add(owner)
        if pid == a_bag_pid:
            bag_frame_count += 1
    check("A 的结算帧里确实有背包帧（前提成立）", bag_frame_count > 0,
          bag_frame_count)
    # ★ 用 RPC **之后** A 的背包 id 集合：发奖新建的格子也是 A 自己的对象
    a_bag_ids_after = set()
    collect_ids(a.props.get("item_bag") or {}, a_bag_ids_after)
    check("A 的帧里出现 A 自己的背包对象 id（非空断言）",
          bool(frame_ids_a & a_bag_ids_after),
          sorted(frame_ids_a & a_bag_ids_after)[:5])
    check("A 的帧里没有 B 独有的对象 id", b_new_id not in frame_ids_a, b_new_id)
    check("A 的属性帧 ownerid 全是空串（= 自己）",
          frame_owners_a <= {""}, frame_owners_a)

    # 反向：B 跑同样的 RPC，帧里不该出现 A 独有的 id
    a_new = story.add_item(a.props, 88888, 1)
    check("A 也新建一个背包格子", bool(a_new) and a_new[2] is True, a_new)
    a_new_id = a.props["item_bag"][a_new[1]]["__id__"]
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                 "modeId": 1, "storyID": 1}]), 4,
                   uid="multi_b")
    frames_b = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                              msmod.mp_arr([FINISH_INFO("multi_b")]), 5,
                              uid="multi_b")
    frame_ids_b = set()
    for f in frames_b:
        if not isinstance(f, (bytes, bytearray)):
            continue
        try:
            _o, _p, _t, _n, ids = frame_info(f)
        except Exception:
            continue
        frame_ids_b |= ids
    check("B 的帧里没有 A 独有的对象 id", a_new_id not in frame_ids_b, a_new_id)

    print("\n[3] ownerid 语义：默认空串；显式写进帧；int/bytes 直接抛")
    f_self = rpc_common.sync_frame(rpc_common.SYNC_SELF, 0, msmod.OP_CHANGE,
                                  "signature", "x")
    owner, _pid, _op, _name, _ids = frame_info(f_self)
    check("SYNC_SELF 是空串", rpc_common.SYNC_SELF == "")
    check("默认帧的 ownerid 为空（= 收帧者自己）", owner == "", repr(owner))
    f_other = rpc_common.sync_frame("multi_b", 0, msmod.OP_CHANGE,
                                   "signature", "y")
    owner2 = frame_info(f_other)[0]
    check("显式 ownerid 会写进帧", owner2 == "multi_b", repr(owner2))
    try:
        rpc_common.sync_frame(None, 0, msmod.OP_CHANGE, "signature", "z")
        check("ownerid=None 当成自己（不抛）", True)
    except Exception as e:
        check("ownerid=None 当成自己（不抛）", False, e)
    for bad in (0, 123, b"multi_a"):
        raised = False
        try:
            rpc_common.sync_frame(bad, 0, msmod.OP_CHANGE, "signature", "w")
        except TypeError:
            raised = True
        check("ownerid=%r 抛 TypeError（不静默转字符串）" % (bad,), raised)

    print("\n[4] 影子缓存按客户端语义处理非空 ownerid（未知 owner 整帧丢弃）")
    pre = pre_snapshot("multi_a")
    sc, errs = frames_safe(pre, [f_other], "别人属性")
    check("非空 ownerid 的帧不会崩", not errs, errs[:2])
    check("影子缓存记下了 ownerid", "multi_b" in sc.ownerids, sc.ownerids)
    check("未知 owner 的帧被整帧丢弃（客户端 getRealOwner 拿不到就 return）",
          sc.owner_frames_dropped == 1 and sc.frame_count == 0,
          (sc.owner_frames_dropped, sc.frame_count))
    sc2, _e2 = frames_safe(pre, [f_self], "自己属性")
    check("自己的帧不记 ownerid", not sc2.ownerids, sc2.ownerids)
    check("自己的帧正常应用", sc2.frame_count == 1, sc2.frame_count)

    print("\n[5] transport/accounts 关联表")
    accounts.release(9001)
    check("未绑定 -> 回退默认 uid", accounts.uid_of(9001) == accounts.DEFAULT_UID)
    accounts.bind(9001, "multi_a")
    check("bind 之后 uid_of 正确", accounts.uid_of(9001) == "multi_a")
    accounts.release(9001)
    check("release 之后回退默认", accounts.uid_of(9001) == accounts.DEFAULT_UID)
    # ★ 审计 P2-7：bind(None) 也必须落键，否则同一 cid 会二次领取别人的账号
    accounts.bind(9002, None)
    check("bind(None) 也算已尝试绑定", accounts.is_bound(9002) is True)
    check("bind(None) 后 uid_of 回退默认",
          accounts.uid_of(9002) == accounts.DEFAULT_UID)
    accounts.release(9002)
    # claim 按先进先出
    accounts.remember_username("multi_x")
    accounts.remember_username("multi_y")
    check("claim 先到先得 1", accounts.claim_username() == "multi_x")
    check("claim 先到先得 2", accounts.claim_username() == "multi_y")
    # 从 auth 里的 mailbox 名字还原
    mb = msmod.pb_bytes_field(1, b"avatar_multi_z")
    check("从 avatar_ 前缀还原", accounts.uid_from_auth({5: [mb]}) == "multi_z")
    mb2 = msmod.pb_bytes_field(1, b"account_multi_w")
    check("从 account_ 前缀还原", accounts.uid_from_auth({5: [mb2]}) == "multi_w")
    check("没有 auth 返回 None", accounts.uid_from_auth({}) is None)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
