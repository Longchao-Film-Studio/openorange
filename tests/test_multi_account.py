#!/usr/bin/env python
"""多账号 / ownerid 帧路由（P0 遗留第 4 条）。

遗留原状：「多账号 / ownerid 的帧路由未建模（当前 30 处 _sync_frame 全传空 ownerid）」。

★ 第三版（两轮审计后）。前两版的问题：
  1. 第一版「帧不交叉」是假阳性（漏收集 A 的 characterBag，且跑的 RPC 不产背包帧）。
  2. 第二版反向断言**仍然恒真**：它用的 sentinel 是 story.add_item 直接改状态产生的 id，
     这个 id 根本不经过任何 RPC，当然不会出现在任何帧里。
  现在改成：先跑 B 的结算拿到 B 帧里**真实出现**的 id，再跑 A，断言 A 的帧里没有这些
  B 独有的 id —— 如果 _state(uid) 串了号，这条断言会立刻红。

用法: python tests/test_multi_account.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_multi_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate
from game_server import rpc_common as rpc_common  # noqa: E402
from game_server.property import sync as prop_sync  # noqa: E402
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
    out = set()
    for k, v in props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            continue
        collect_ids(v, out)
    return out


def sync_frame_info(frame):
    """只解析属性同步帧(type=13)，返回 (ownerid, pid, optype, name, ids)；其它帧返回 None。"""
    f = bytes(frame)
    if len(f) < 6:
        return None
    if int.from_bytes(f[4:6], "little") != msmod.PROPERTY_SYNC_TYPE:
        return None
    fields = msmod.pb_parse(f[6:])
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


PROLOGUE_MATCH = {"match_type": 3, "system_type": 1004, "modeId": 1, "storyID": 1}


def finish_info(uid):
    return {
        "toStreetId": 101, "toStreetPosId": 0, "finishReason": 0,
        uid: {"win": True, "useTime": 60, "collectCount": 0,
              "battle_items": {"201": 5}},
    }


def run_one_side(uid):
    """给某个账号跑「进副本 -> 结算发奖」，返回 (帧, 帧内 id, ownerid 集合, 背包帧数)。"""
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([PROLOGUE_MATCH]), 2, uid=uid)
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                            msmod.mp_arr([finish_info(uid)]), 3, uid=uid)
    ids, owners = set(), set()
    bag_frames = 0
    st = pstate.get_state(uid=uid)
    bag_pid = st.props["item_bag"]["__id__"]
    for f in frames:
        if not isinstance(f, (bytes, bytearray)):
            continue
        info = sync_frame_info(f)
        if info is None:
            continue
        owner, pid, _op, _n, vids = info
        ids |= vids
        owners.add(owner)
        if pid == bag_pid:
            bag_frames += 1
    return frames, ids, owners, bag_frames


def run():
    del passed[:]
    del failed[:]
    tag = f"{random.randint(0, 0xFFFF):04x}"
    ua, ub = "ma_" + tag, "mb_" + tag

    print("\n[1] 状态隔离：A 的 RPC 不改 B 的状态")
    a = state(ua)
    b = state(ub)
    b_street_before = b.props.get("street_id")
    b_bag_before = sorted((k, v.get("num")) for k, v in
                          (b.props.get("item_bag") or {}).items()
                          if isinstance(k, str) and not k.startswith("__"))
    guarded_handle(erpc.RPC_INDEX["RequestSwitchStreet_CS"],
                   msmod.mp_arr([1011, None]), 1, uid=ua)
    check("A 切到 1011", a.props.get("street_id") == 1011, a.props.get("street_id"))
    check("B 的 street_id 没变", b.props.get("street_id") == b_street_before,
          (b_street_before, b.props.get("street_id")))
    b_bag_after = sorted((k, v.get("num")) for k, v in
                         (b.props.get("item_bag") or {}).items()
                         if isinstance(k, str) and not k.startswith("__"))
    check("B 的背包没变", b_bag_before == b_bag_after,
          (len(b_bag_before), len(b_bag_after)))

    print("\n[2] 帧隔离（双向可失败）：先跑 B 拿到 B 帧里真实出现的 id，再跑 A")
    a_ids_before = props_ids(a.props)
    b.props["_oo_next_id"] = max(a_ids_before) + 1000
    _fb, ids_b, owners_b, bag_frames_b = run_one_side(ub)
    check("B 的结算确实产生了背包帧", bag_frames_b > 0, bag_frames_b)
    check("B 的帧里有对象 id（前提成立）", bool(ids_b), sorted(ids_b)[:5])
    b_only = ids_b - props_ids(a.props)
    check("存在 B 独有的帧内 id（前提成立）", bool(b_only), sorted(b_only)[:5])

    _fa, ids_a, owners_a, bag_frames_a = run_one_side(ua)
    check("A 的结算确实产生了背包帧", bag_frames_a > 0, bag_frames_a)
    a_bag_ids_after = set()
    collect_ids(a.props.get("item_bag") or {}, a_bag_ids_after)
    check("A 的帧里出现 A 自己的背包对象 id（非空断言）",
          bool(ids_a & a_bag_ids_after), sorted(ids_a & a_bag_ids_after)[:5])
    check("★ A 的帧里没有任何 B 独有的帧内 id（串号会在这里红）",
          not (ids_a & b_only), sorted(ids_a & b_only)[:5])
    check("A 的属性帧 ownerid 全是空串（= 自己）", owners_a <= {""}, owners_a)
    check("B 的属性帧 ownerid 全是空串（= 自己）", owners_b <= {""}, owners_b)

    print("\n[3] ownerid 语义：默认空串；显式写进帧；int/bytes 抛 TypeError")
    f_self_frame = rpc_common.sync_frame(prop_sync.SYNC_SELF, 0, msmod.OP_CHANGE,
                                        "signature", "x")
    info = sync_frame_info(f_self_frame)
    check("SYNC_SELF 是空串", prop_sync.SYNC_SELF == "")
    check("默认帧的 ownerid 为空（= 收帧者自己）", info[0] == "", info[0])
    f_other = rpc_common.sync_frame("someone_else", 0, msmod.OP_CHANGE,
                                    "signature", "y")
    check("显式 ownerid 会写进帧", sync_frame_info(f_other)[0] == "someone_else")
    try:
        rpc_common.sync_frame(None, 0, msmod.OP_CHANGE, "signature", "z")
        check("ownerid=None 当成自己（不抛）", True)
    except Exception as e:
        check("ownerid=None 当成自己（不抛）", False, e)
    for bad in (0, 123, b"x"):
        raised = False
        try:
            rpc_common.sync_frame(bad, 0, msmod.OP_CHANGE, "signature", "w")
        except TypeError:
            raised = True
        check(f"ownerid={bad!r} 抛 TypeError（不静默转字符串）", raised)

    print("\n[4] 影子缓存按客户端语义处理非空 ownerid（未知 owner 整帧丢弃）")
    pre = pre_snapshot(ua)
    sc, errs = frames_safe(pre, [f_other], "别人属性")
    check("非空 ownerid 的帧不会崩", not errs, errs[:2])
    check("影子缓存记下了 ownerid", "someone_else" in sc.ownerids, sc.ownerids)
    check("未知 owner 的帧被整帧丢弃（客户端 getRealOwner 拿不到就 return）",
          sc.owner_frames_dropped == 1 and sc.frame_count == 0,
          (sc.owner_frames_dropped, sc.frame_count))
    sc2, errs2 = frames_safe(pre, [f_self_frame], "自己属性")
    check("自己的帧不记 ownerid", not sc2.ownerids, sc2.ownerids)
    check("自己的帧正常应用且不报错", sc2.frame_count == 1 and not errs2,
          (sc2.frame_count, errs2[:2]))

    print("\n[5] transport/accounts 关联表")
    accounts.release(9001)
    check("未绑定 -> 回退默认 uid", accounts.uid_of(9001) == accounts.DEFAULT_UID)
    accounts.bind(9001, "ua")
    check("bind 之后 uid_of 正确", accounts.uid_of(9001) == "ua")
    accounts.release(9001)
    check("release 之后回退默认", accounts.uid_of(9001) == accounts.DEFAULT_UID)
    accounts.bind(9002, None)
    check("bind(None) 也算已尝试绑定", accounts.is_bound(9002) is True)
    check("bind(None) 后 uid_of 回退默认",
          accounts.uid_of(9002) == accounts.DEFAULT_UID)
    accounts.release(9002)
    accounts.release(9003)
    got = accounts.claim_and_bind(9003)
    check("空队列 claim_and_bind 返回 None", got is None, got)
    check("空队列也会落键（is_bound=True）", accounts.is_bound(9003) is True)
    accounts.remember_username("cb_first")
    check("已绑的 cid 再 claim_and_bind 不会抢号",
          accounts.claim_and_bind(9003) is None)
    check("队列里的号还在", accounts.claim_username() == "cb_first")
    accounts.release(9003)
    accounts.remember_username("mx1")
    accounts.remember_username("mx2")
    check("claim 先到先得 1", accounts.claim_username() == "mx1")
    check("claim 先到先得 2", accounts.claim_username() == "mx2")
    mb = msmod.pb_bytes_field(1, b"avatar_mz")
    check("从 avatar_ 前缀还原", accounts.uid_from_auth({5: [mb]}) == "mz")
    mb2 = msmod.pb_bytes_field(1, b"account_mw")
    check("从 account_ 前缀还原", accounts.uid_from_auth({5: [mb2]}) == "mw")
    check("没有 auth 返回 None", accounts.uid_from_auth({}) is None)

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
