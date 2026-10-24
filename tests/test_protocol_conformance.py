#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""协议符合性测试（审计 goal-0ab72855 第四遍）。

用「客户端视角」的参数构造打服务端 handler，断言客户端回调能拿到它要的字段：
  T1 UpgradeCharacter_CS：客户端传 {charId, sendItems}——服务端必须扣道具（现在不扣=经济漏洞，先红）
  T2 UseItem_CS 失败分支：errorcode 必须是客户端 ErrorConst 枚举值（现在泛化，先红）
  T3 RpcPlayActEmoji：表情必须广播（现在只 ACK，先红）
  T4 Set* 系列：根属性同步帧 propertyId=0 + 落库（回归）
  T5 取名三件套：player_name/gender 同步帧（回归）

用法: python tests/test_protocol_conformance.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_conf_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import rpc_index  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
_TAG = "conf"


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  [预期红] " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return "%s_%s" % (name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def call(rpc_name, args, rid, uid):
    return guarded_handle(rpc_index.RPC_INDEX[rpc_name], msmod.mp_arr(args), rid, uid=uid)


def frames_of(ret):
    """guarded_handle 返回 (resp, frames) 或 frames。兼容两种。"""
    if isinstance(ret, tuple) and len(ret) == 2:
        return ret[1]
    return ret


def resp_of(ret):
    return ret[0] if isinstance(ret, tuple) else ret


# ---------------- T1: UpgradeCharacter 扣道具 ----------------
def t1():
    print("T1 UpgradeCharacter_CS: sendItems 必须被扣（客户端视角 {charId, sendItems}）")
    st = fresh("t1")
    bag = st.get("characterBag") or {}
    cid = None
    for k in bag:
        if str(k).isdigit() and isinstance(bag[k], dict):
            cid = k
            break
    if cid is None:
        check("T1.prep 有角色", False, "characterBag 空")
        return
    # ★ 修复后语义：sendItems 必须是升级材料（ItemData.useParam.CharExp）。
    #   给 T1 塞真材料 201×2（CharExp=200/个，升 1 级要 200exp → 1 个就够，
    #   2 个连升 + 溢出进 CharExp，顺带验证逐级结算）。
    bag = st.get("characterBag")
    bag["9001"] = bag.get("9001") or {"__id__": 990001}
    st.set("characterBag", bag)
    bag_items = st.get("item_bag") or {}
    bag_items["900"] = {"__id__": 990002, "id": "900", "itemId": 201, "num": 2}
    st.set("item_bag", bag_items)
    iid = "900"
    send_items = {201: 2}
    level_before = int((bag[cid] or {}).get("CharLevel") or 1)
    item_before = 2
    ret = call("UpgradeCharacter_CS", [cid, send_items], 101, uid_of("t1"))
    st2 = pstate.get_state(uid=uid_of("t1"))
    bag2 = st2.get("characterBag") or {}
    ch = (bag2.get(cid) or bag2.get(str(cid)) or {}) if isinstance(bag2, dict) else {}
    level_after = int(ch.get("CharLevel") or 1)
    check("T1.level 增量>=1", level_after > level_before,
          "before=%s after=%s" % (level_before, level_after))
    if iid and item_before is not None:
        ib2 = st2.get("item_bag") or {}
        it2 = ib2.get(iid) or ib2.get(int(iid)) or {}
        item_after = int(it2.get("num") or 0)
        check("T1.items 道具被扣", item_after < item_before,
              "before=%s after=%s" % (item_before, item_after))
    s = str(resp_of(ret))
    check("T6 应答含 status", "status" in s, s[:120])

    # ★ 负例：非升级材料（101 资历）不能当材料提交 —— 服务端必须拒绝
    #   （这是审计 T1 的另一半：sendItems 里混入非法 itemId 时不白扣不白升）
    ret_bad = call("UpgradeCharacter_CS", [cid, {101: 5}], 106, uid_of("t1"))
    sb = str(resp_of(ret_bad))
    check("T1.负例 非材料道具被拒", "errorcode" in sb, sb[:140])


# ---------------- T2: UseItem errorcode ----------------
def t2():
    print("T2 UseItem_CS: 失败分支 errorcode 应为客户端 ErrorConst 枚举")
    fresh("t2")
    ret = call("UseItem_CS", [99999999, 1, None], 102, uid_of("t2"))
    s = str(resp_of(ret))
    # 客户端 UseItem_SC 按 ErrorConst.TID_SKIN_HAS_NO_CHAR / Bag_LevelNotEnough 分支；
    # 泛化 errorcode=1 命中不了任何分支。
    import re as _re
    m = _re.search(r"errorcode[^,}]{0,20}", s)
    val = m.group(0) if m else "(无 errorcode 字段)"
    check("T2.errorcode 非泛化", ("errorcode" in s) and (": 1" not in val) and ("= 1" not in val) and ("1\\x" not in val), val)


# ---------------- T3: 表情广播 ----------------
def t3():
    print("T3 RpcPlayActEmoji: 应广播（同空间第二玩家可见）")
    fresh("t3")
    # ★ 协议取证：PlayActEmoji 是 CallServer 单向（avataraoicomp.lua，rid=0）。
    #   之前测试用 rid=103 → dispatch 把它当"未知带回调 RPC"落进通用兜底（红的原因）。
    ret = call("RpcPlayActEmoji", [1, 1001], 0, uid_of("t3"))
    fr = frames_of(ret)
    txt = str(fr)
    # 帧存在且不是 "not implemented" 的错误应答（弱断言会漏：错误应答也是帧）
    is_not_impl = "not implemented" in txt
    check("T3 有真广播帧", (fr is not None) and (hasattr(fr, "__len__") and len(fr) > 0) and not is_not_impl,
          ("len=%s" % (len(fr) if hasattr(fr, "__len__") else "?")) + (" not_implemented" if is_not_impl else "") + " " + txt[:100])


# ---------------- T4: Set* 根属性帧 ----------------
def t4():
    print("T4 Set* 系列: 根属性同步帧 propertyId=0 + 落库")
    u = uid_of("t4")
    fresh("t4")
    pre = msmod._live_init_dict(u)   # RPC 之前的 initDict 快照（frames_safe 要求）
    ret = call("SetSignature_CS", ["审计签名"], 104, uid=u)
    fr = frames_of(ret)
    check("T4.帧 非空", bool(fr), str(fr)[:100])
    st2 = pstate.get_state(uid=u)
    check("T4.落库", st2.get("signature") == "审计签名", st2.get("signature"))
    sc, errs = frames_safe(pre, fr, "t4")
    check("T4.影子缓存不崩", not errs and not sc.warnings,
          (str(errs[:1])[:120], str(sc.warnings[:1])[:120]))


# ---------------- T5: 取名回归 ----------------
def t5():
    print("T5 取名三件套: player_name/gender 同步帧（回归）")
    u = uid_of("t5")
    fresh("t5")
    pre = msmod._live_init_dict(u)
    ret = call("RpcCommitNameGender", ["审计名", 2], 105, uid=u)
    fr = frames_of(ret)
    txt = str(fr)
    check("T5.player_name 帧", "player_name" in txt, txt[:120])
    check("T5.gender 帧", "gender" in txt, txt[:120])
    sc, errs = frames_safe(pre, fr, "t5")
    check("T5.影子缓存不崩", not errs and not sc.warnings,
          (str(errs[:1])[:120], str(sc.warnings[:1])[:120]))


def main():
    print("=== 协议符合性测试（客户端视角）===")
    for t in (t1, t2, t3, t4, t5):
        try:
            t()
        except Exception as e:
            check(t.__name__ + " 无异常", False, repr(e)[:150])
        print()
    # 预期红（先红后修，审计 goal-0ab72855 §6）：修复前这 3 个 FAIL 是审计结论本身，
    # 不算套件失败。修复后应把对应项从 EXPECTED_RED 移除。
    # T1/T2/T3 已全部修复（2026-10-24，各自独立提交）
    EXPECTED_RED = set()
    unexpected = [f for f in failed if f not in EXPECTED_RED]
    red = [f for f in failed if f in EXPECTED_RED]
    print("=== 结果: PASS=%d FAIL=%d ===" % (len(passed), len(unexpected)))
    print("通过 %d 项，失败 %d 项（另有 %d 项预期红，为审计实锤缺口，修复前属结论本身）"
          % (len(passed), len(unexpected), len(red)))
    for f in red:
        print("  预期红(待修):", f)
    for f in unexpected:
        print("  FAIL:", f)
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
