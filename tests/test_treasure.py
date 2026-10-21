#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""街景宝箱奖励测试。

逆向依据（BDD StreetTreasureTaskData，44 条）：
  AwardItemList = {道具id: 数量}                 —— 38/44 条有
  AwardAttr     = {street_id, prestige_exp}      —— 30/44 条有（都是 +6）

2026-10-21 修：之前只把 treasure_box_info[tid] 标成 1，**奖励一个没发** ——
玩家点开箱子只看到开箱动画，没道具也没声望。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_treasure_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,
                         config, gamedata)

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid):
    config.configure_scaffold(unlock_all=False)
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


def bag_items(st):
    bag = st.props.get("item_bag") or {}
    out = {}
    for k, v in bag.items():
        if isinstance(v, dict) and isinstance(v.get("itemId"), int):
            out[v["itemId"]] = v.get("num")
    return out


def street_entry(st, sid=101):
    return (st.props.get("street_info") or {}).get(sid) or {}


def run():
    print("\n[1] 宝箱配置（BDD）")
    cfg = gamedata.street_treasure(10157) or {}
    check("10157 存在", bool(cfg))
    ai = cfg.get("AwardItemList") or {}
    check("AwardItemList 有道具 203 x2",
          ai.get("203") == 2, ai)
    aa = cfg.get("AwardAttr") or {}
    check("AwardAttr.prestige_exp = 6", aa.get("prestige_exp") == 6, aa)
    check("AwardAttr.street_id = 101", aa.get("street_id") == 101, aa)

    print("\n[2] 开箱发道具 + 声望经验")
    st = fresh("tr_a")
    before = bag_items(st)
    _pre = msmod._live_init_dict("tr_a")   # RPC 前快照（审计 P0-3）
    frames = erpc.handle(erpc.RPC_INDEX["RequestStreetTreasure_CS"],
                         msmod.mp_arr([10157]), 1, uid="tr_a")
    after = bag_items(st)
    check("道具 203 从无到 2",
          before.get(203) is None and after.get(203) == 2,
          (before.get(203), after.get(203)))
    entry = street_entry(st)
    check("声望经验 +6",
          int(entry.get("prestige_exp") or 0) == 6, entry.get("prestige_exp"))
    check("宝箱标记为已开",
          (entry.get("treasure_box_info") or {}).get(10157) == 1)
    check("返回同步帧（不止宝箱状态）", len(frames) >= 3, len(frames))
    # ★ 影子缓存校验：开宝箱的帧喂进客户端会不会崩（审计说这条路径曾发带 __tp__ 的
    #   整条 StreetInfo，客户端 rochange 索引 nil）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shadow_cache import frames_safe
    _sc, _errs = frames_safe(_pre, frames, "开宝箱")
    check("开宝箱的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n[3] 重复开启不再发奖")
    frames2 = erpc.handle(erpc.RPC_INDEX["RequestStreetTreasure_CS"],
                          msmod.mp_arr([10157]), 2, uid="tr_a")
    after2 = bag_items(st)
    check("道具数量没变", after2.get(203) == 2, after2.get(203))
    entry2 = street_entry(st)
    check("声望经验没变",
          int(entry2.get("prestige_exp") or 0) == 6, entry2.get("prestige_exp"))
    check("第二次同步帧更少", len(frames2) < len(frames), (len(frames2), len(frames)))

    print("\n[4] 单向 RequestTouchTreasure_CS 也发奖")
    st2 = fresh("tr_b")
    erpc.handle(1282426820, msmod.mp_arr([10158]), 0, uid="tr_b")
    after3 = bag_items(st2)
    cfg2 = gamedata.street_treasure(10158) or {}
    ai2 = cfg2.get("AwardItemList") or {}
    first_iid = int([k for k in ai2 if k not in ("KeyList", "__bdd_metatype__")][0])
    check("道具 %d 已发放" % first_iid, after3.get(first_iid) is not None, after3)
    entry3 = street_entry(st2)
    check("声望经验 +6",
          int(entry3.get("prestige_exp") or 0) == 6, entry3.get("prestige_exp"))

    print("\n[5] 没奖励的宝箱不崩")
    # 10161 / 10173 是完全没有 AwardItemList/AwardAttr 的两条
    st3 = fresh("tr_c")
    erpc.handle(erpc.RPC_INDEX["RequestStreetTreasure_CS"],
                msmod.mp_arr([10161]), 3, uid="tr_c")
    entry4 = street_entry(st3)
    check("标记为已开",
          (entry4.get("treasure_box_info") or {}).get(10161) == 1)
    check("声望没变", int(entry4.get("prestige_exp") or 0) == 0,
          entry4.get("prestige_exp"))

    print("\n[6] 声望经验能触发升级（needExpNum=100）")
    st4 = fresh("tr_d")
    # 连开多个箱子攒经验
    for i, tid in enumerate([10141, 10142, 10143, 10144, 10145, 10146,
                             10147, 10148, 10149, 10150, 10151, 10152,
                             10153, 10154, 10155, 10156, 10157, 10158]):
        erpc.handle(erpc.RPC_INDEX["RequestStreetTreasure_CS"],
                    msmod.mp_arr([tid]), 10 + i, uid="tr_d")
    entry5 = street_entry(st4)
    lv = int(entry5.get("prestige_level") or 0)
    check("声望等级 > 0（经验累积触发了升级）", lv > 0, lv)
    check("经验值在合理范围（0~99）",
          0 <= int(entry5.get("prestige_exp") or 0) < 100,
          entry5.get("prestige_exp"))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
