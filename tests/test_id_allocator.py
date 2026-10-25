#!/usr/bin/env python
"""P0-A：单一 __id__ 分配器测试。

背景（2026-10-21 审计）：
  项目里原本有**三个**互不相知的 __id__ 分配器：
    PlayerState.next_property_id()   -> 缓存 property_seq
    story._next_id_for_props()       -> 每次扫 props 求 max
    scaffold._IdGen(_max_id(props))  -> 自己扫 max + 自增
  实测撞号：1381 同时属于「item_bag 格子」和「done_story_tasks 条目」。

  现在统一到 game_server/property/ids.py 的 alloc_id(props)，
  分配进度存在 props["_oo_next_id"]（_oo_ 前缀，客户端看不到）。

测试用独立临时存档目录，不碰真实存档。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_ids_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, scaffold, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from game_server.property import ids as ids_mod
from tests.frame_guard import check_frames, pre_snapshot

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


def bag_ids(st):
    bag = st.props.get("item_bag") or {}
    return [v.get("__id__") for k, v in bag.items()
            if isinstance(v, dict) and isinstance(v.get("__id__"), int)]


def run():
    print("\n[1] max_id 基础")
    p = {"a": {"__id__": 1000}, "b": {"__id__": 1377}, "c": [{"__id__": 1200}]}
    check("max_id 找到最大 1377", ids_mod.max_id(p) == 1377, ids_mod.max_id(p))
    check("空 dict 返回 0", ids_mod.max_id({}) == 0)
    check("list 也递归", ids_mod.max_id([{"__id__": 5}, {"__id__": 9}]) == 9)

    print("\n[2] alloc_id 单调递增")
    p2 = {"x": {"__id__": 100}}
    a1 = ids_mod.alloc_id(p2)
    a2 = ids_mod.alloc_id(p2)
    a3 = ids_mod.alloc_id(p2)
    check("首个 = max+1 = 101", a1 == 101, a1)
    check("连续分配不重复", len({a1, a2, a3}) == 3, (a1, a2, a3))
    check("进度写进 props[_oo_next_id]", p2.get("_oo_next_id") == 103,
          p2.get("_oo_next_id"))
    check("peek_next_id 一致", ids_mod.peek_next_id(p2) == 103)

    print("\n[3] verify_unique")
    ok, dup = ids_mod.verify_unique({"a": {"__id__": 7}, "b": {"__id__": 8}})
    check("正常 props 唯一", ok is True and not dup, dup)
    ok2, dup2 = ids_mod.verify_unique({"a": {"__id__": 7}, "b": {"__id__": 7}})
    check("撞号能被检出", ok2 is False and 7 in dup2, dup2)

    print("\n[4] ★ 三个入口共用同一个计数器")
    # 审计指出：原测试用的 itemId 101 初始背包里已经有，走的是叠加分支，
    # 根本没分配新 id —— 断言只是碰巧成立。改用初始背包里没有的 itemId。
    st = fresh("ids_a")
    before = ids_mod.peek_next_id(st.props)
    bag0 = st.props.get("item_bag") or {}
    have = {g.get("itemId") for g in bag0.values() if isinstance(g, dict)}
    new_item = next(i for i in range(90001, 90200) if i not in have)
    n1 = st.next_property_id()                    # 入口 1：PlayerState
    n2 = ids_mod.alloc_id(st.props)               # 入口 2：直接调
    _num, gkey, is_new = story.add_item(st.props, new_item, 1)   # 入口 3
    n3 = (st.props["item_bag"][gkey].get("__id__")) if is_new else None
    check("入口 3 确实新建了格子（不是叠加）", is_new is True, (gkey, is_new))
    check("三个入口的 id 都递增且互不相同",
          n3 is not None and len({n1, n2, n3}) == 3 and n1 > before,
          (before, n1, n2, n3))
    check("计数器推进了 3", ids_mod.peek_next_id(st.props) >= before + 3,
          (before, ids_mod.peek_next_id(st.props)))

    print("\n[5] ★ 回归：审计里的撞号场景（真实发奖路径）")
    # 审计指出：story.finish_story_task 只写 done_story_tasks、**不发奖**，
    # 真正建背包格子的是 entity_rpc._finish_story_tasks -> story_award -> add_item。
    # 这里必须走真实发奖路径才能复现撞号场景。
    st2 = fresh("ids_b")
    bag_before = len(bag_ids(st2))
    story.start_story_task(st2, 1005, 0)
    story.start_story_task(st2, 1003, 0)
    st2.save()
    # ★ 审计 P2-9：这条路径以前直接调 handler 并把帧丢掉，没有任何影子缓存校验。
    #   发奖/建背包格子正是最容易产生新 __id__ 和属性帧的地方，必须过一遍客户端模型。
    _pre = pre_snapshot("ids_b")
    _frames = erpc._finish_story_tasks(st2, [1005])
    _frames += erpc._finish_story_tasks(st2, [1003])
    st2.save()
    check("真实发奖路径产生了属性帧", bool(_frames), len(_frames or []))
    # ★ 审计 P2-10：只断言「有帧」不够 —— 任务帧也会让这条绿。
    #   必须锁住「确实推了背包格子的 OP_ADD 帧」，否则发奖不推背包（客户端看不到道具）也测不出来。
    _bag_pid = st2.props["item_bag"]["__id__"]
    _bag_add = 0
    for _f in (_frames or []):
        if not isinstance(_f, (bytes, bytearray)) or len(_f) < 6:
            continue
        try:
            _fl = msmod.pb_parse(bytes(_f[6:]))
        except Exception:
            continue
        if _fl.get(2, [None])[0] == _bag_pid and _fl.get(3, [None])[0] == msmod.OP_ADD:
            _bag_add += 1
    check("发奖推了背包格子的 OP_ADD 帧", _bag_add > 0, _bag_add)
    try:
        check_frames(_pre, _frames, "真实发奖")
        check("真实发奖的帧在客户端不会崩", True)
    except AssertionError as _e:
        check("真实发奖的帧在客户端不会崩", False, str(_e)[:160])
    bag_after = len(bag_ids(st2))
    check("真实发奖路径确实新建了背包格子", bag_after > bag_before,
          (bag_before, bag_after))
    ok, dup = ids_mod.verify_unique(st2.props)
    check("完成两个任务并真实发奖后无重复 __id__", ok is True, dup)
    check("背包格子 id 各不相同",
          len(bag_ids(st2)) == len(set(bag_ids(st2))), bag_ids(st2))

    print("\n[6] 脚手架入口也共用")
    # 审计指出：原测试传的是 PlayerState 而不是 props（会 TypeError），
    # 且默认 all_tasks_done=False 时 scaffold.apply 直接返回、什么都没种。
    st3 = fresh("ids_c")
    # ★ fresh() 内部会 configure_scaffold(unlock_all=False)，所以必须在它之后再开
    config.configure_scaffold(unlock_all=True)
    applied = scaffold.apply(st3.props)
    check("脚手架确实种了数据", bool(applied), applied[:3])
    ok3, dup3 = ids_mod.verify_unique(st3.props)
    check("脚手架种完数据后无重复 __id__", ok3 is True, dup3)
    check("脚手架也推进了同一个计数器",
          ids_mod.peek_next_id(st3.props) >= ids_mod.max_id(st3.props),
          (ids_mod.peek_next_id(st3.props), ids_mod.max_id(st3.props)))
    config.configure_scaffold(unlock_all=False)   # 复位

    print("\n[7] 存档往返：_oo_next_id 持久化")
    st4 = fresh("ids_d")
    x1 = st4.next_property_id()
    st4.save()
    # 清内存缓存，从盘里重新读
    pstate._STATES.pop("ids_d", None)
    st4b = pstate.get_state(uid="ids_d")
    x2 = st4b.next_property_id()
    check("重启后继续递增（不回退）", x2 > x1, (x1, x2))
    check("_oo_next_id 跟着存档落盘",
          isinstance(st4b.props.get("_oo_next_id"), int),
          st4b.props.get("_oo_next_id"))

    print("\n[8] 旧存档（没有 _oo_next_id）能正确接上")
    st5 = fresh("ids_e")
    st5.props.pop("_oo_next_id", None)          # 模拟旧存档
    mx = ids_mod.max_id(st5.props)
    n = ids_mod.alloc_id(st5.props)
    check("旧存档首个分配 = max+1", n == mx + 1, (mx, n))

    print("\n[9] _oo_next_id 不下发给客户端")
    st6 = fresh("ids_f")
    st6.next_property_id()
    st6.save()
    d = msmod._live_init_dict("ids_f")
    pub = d.get("__Properties__") or {}
    check("initDict 里没有 _oo_next_id", "_oo_next_id" not in pub,
          [k for k in pub if str(k).startswith("_oo_")])
    check("props 里确实有（只是被过滤）",
          isinstance(st6.props.get("_oo_next_id"), int))

    print("\n[10] ★ 加载期校正（审计 P0-2）")
    # 审计实测：counter 偏小时 alloc_id 会返回已存在的 id。
    # reconcile 必须在读档时把它抬到真实 max。
    p_bad = {"a": {"__id__": 500}, "_oo_next_id": 100}
    fixed, dup = ids_mod.reconcile(p_bad, warn=False)
    check("reconcile 抬正了计数器", fixed is True and p_bad["_oo_next_id"] == 500,
          p_bad.get("_oo_next_id"))
    check("校正后分配 > 真实 max", ids_mod.alloc_id(p_bad) == 501)

    p_ok = {"a": {"__id__": 10}, "_oo_next_id": 999}
    fixed2, _d2 = ids_mod.reconcile(p_ok, warn=False)
    check("counter 已经够大时不动它", fixed2 is False and p_ok["_oo_next_id"] == 999)

    p_dup = {"a": {"__id__": 7}, "b": {"__id__": 7}}
    _f3, dup3 = ids_mod.reconcile(p_dup, warn=False)
    check("reconcile 能报出已有撞号", 7 in dup3, dup3)

    print("\n[11] ★ GM 工具不再独立发号（审计 P0-1）")
    # 审计端到端复现：tools/give_item.py 自己算 max+1、不更新 counter，
    # 发完道具重登后第一次分配就撞号。现在它必须走 alloc_id。
    from tools import give_item as _give_item
    st7 = fresh("ids_g")
    before7 = ids_mod.peek_next_id(st7.props)
    gkey7, _n7, how7 = _give_item.give(st7.props, 90001, 1)
    after7 = ids_mod.peek_next_id(st7.props)
    check("give_item 确实新建了格子", how7 == "新格子", how7)
    check("give_item 推进了统一计数器", after7 > before7, (before7, after7))
    ok7, dup7 = ids_mod.verify_unique(st7.props)
    check("发完道具后 props 无重复 __id__", ok7 is True, dup7)
    # 模拟「重登」：重新读档后第一次分配必须不撞
    st7.save()
    pstate._STATES.pop("ids_g", None)
    st7b = pstate.get_state(uid="ids_g")
    nid7 = st7b.next_property_id()
    ok8, dup8 = ids_mod.verify_unique(st7b.props)
    check("重登后首次分配不撞号", nid7 > before7 and ok8 is True,
          (nid7, before7, dup8))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
