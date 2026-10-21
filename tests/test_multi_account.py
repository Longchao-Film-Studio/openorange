#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多账号 / ownerid 帧路由（P0 遗留第 4 条）。

遗留原状：「多账号 / ownerid 的帧路由未建模（当前 30 处 _sync_frame 全传空
ownerid，不是当前 bug）」。也就是说：

  * 帧里的 ownerid 一直是空串（= 收帧者自己），从没验过这个语义；
  * 多账号的 uid 关联只在 transport/accounts.py 里跑过，没有回归用例；
  * 两个账号的状态/帧会不会串，没人测过。

这个文件把这些补上：
  1. 状态隔离：A 的 RPC 不改 B 的状态
  2. 帧隔离：A 的帧里不出现只属于 B 的对象 __id__
  3. ownerid 语义：默认空串（自己）；显式传 uid 时帧里真的带上
  4. 影子缓存能看见 ownerid
  5. transport/accounts 的关联表：bind / uid_of / release / claim 顺序 / 从 auth 还原

用法: python tests/test_multi_account.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_multi_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, player_state as pstate  # noqa: E402
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
    """递归收集一个值里所有 __id__（用来判断帧有没有引用别人的对象）。"""
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


def run():
    print("\n[1] 状态隔离：A 的 RPC 不改 B 的状态")
    a = state("multi_a")
    b = state("multi_b")
    b_street_before = b.props.get("street_id")
    b_bag_before = dict((k, v) for k, v in (b.props.get("item_bag") or {}).items()
                        if isinstance(k, str) and not k.startswith("__"))
    frames_a = guarded_handle(
        __import__("game_server.entity_rpc", fromlist=["x"]).RPC_INDEX["RequestSwitchStreet_CS"],
        msmod.mp_arr([1011, None]), 1, uid="multi_a")
    check("A 切到 1011", a.props.get("street_id") == 1011, a.props.get("street_id"))
    check("B 的 street_id 没变", b.props.get("street_id") == b_street_before,
          (b_street_before, b.props.get("street_id")))
    b_bag_after = dict((k, v) for k, v in (b.props.get("item_bag") or {}).items()
                       if isinstance(k, str) and not k.startswith("__"))
    check("B 的背包没变", b_bag_before == b_bag_after,
          (len(b_bag_before), len(b_bag_after)))

    print("\n[2] 帧隔离：A 的帧里不出现只属于 B 的对象 __id__")
    b_ids = set()
    collect_ids(b.props.get("item_bag") or {}, b_ids)
    collect_ids(b.props.get("characterBag") or {}, b_ids)
    a_ids = set()
    collect_ids(a.props.get("item_bag") or {}, a_ids)
    only_b = b_ids - a_ids
    check("B 确实有 A 没有的对象 id（前提成立）", bool(only_b), len(only_b))
    leaked = set()
    for f in frames_a:
        if not isinstance(f, (bytes, bytearray)):
            continue
        try:
            _o, _p, _t, _n, ids = frame_info(f)
        except Exception:
            continue
        leaked |= (ids & only_b)
    check("A 的帧里没有 B 的对象 id", not leaked, sorted(leaked)[:5])

    print("\n[3] ownerid 语义：默认空串，显式传 uid 时帧里带 ownerid")
    f_self = rpc_common.sync_frame(rpc_common.SYNC_SELF, 0, msmod.OP_CHANGE,
                                  "signature", "x")
    owner, pid, op, name, _ids = frame_info(f_self)
    check("SYNC_SELF 是空串", rpc_common.SYNC_SELF == "")
    check("默认帧的 ownerid 为空（= 收帧者自己）", owner == "", repr(owner))
    f_other = rpc_common.sync_frame("multi_b", 0, msmod.OP_CHANGE,
                                   "signature", "y")
    owner2, _p2, _o2, _n2, _i2 = frame_info(f_other)
    check("显式 ownerid 会写进帧", owner2 == "multi_b", repr(owner2))
    # 传 None / int 也不该炸（以前会在 pb_bytes_field 里抛 TypeError）
    try:
        rpc_common.sync_frame(None, 0, msmod.OP_CHANGE, "signature", "z")
        check("ownerid=None 不抛异常", True)
    except Exception as e:
        check("ownerid=None 不抛异常", False, e)

    print("\n[4] 影子缓存能看见 ownerid")
    pre = pre_snapshot("multi_a")
    sc, errs = frames_safe(pre, [f_other], "别人属性")
    check("非空 ownerid 的帧不会崩", not errs, errs[:2])
    check("影子缓存记下了 ownerid", "multi_b" in sc.ownerids, sc.ownerids)
    sc2, errs2 = frames_safe(pre, [f_self], "自己属性")
    check("自己的帧不记 ownerid", not sc2.ownerids, sc2.ownerids)

    print("\n[5] transport/accounts 关联表")
    accounts.release(9001)
    check("未绑定 -> 回退默认 uid", accounts.uid_of(9001) == accounts.DEFAULT_UID)
    accounts.bind(9001, "multi_a")
    check("bind 之后 uid_of 正确", accounts.uid_of(9001) == "multi_a")
    accounts.release(9001)
    check("release 之后回退默认", accounts.uid_of(9001) == accounts.DEFAULT_UID)
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
