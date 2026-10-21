#!/usr/bin/env python
# -*- coding: utf-8 -*-
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

from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,
                         story, scaffold, config, gamedata)
from game_server.property import ids as ids_mod

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
    st = fresh("ids_a")
    before = ids_mod.peek_next_id(st.props)
    n1 = st.next_property_id()                 # 入口 1：PlayerState
    n2 = ids_mod.alloc_id(st.props)            # 入口 2：直接调
    got = story.add_item(st.props, 101, 1)     # 入口 3：story.add_item
    n3 = got[1] and (st.props["item_bag"][got[1]].get("__id__"))
    check("三个入口的 id 都递增且不同",
          len({n1, n2, n3}) == 3 and n1 > before,
          (before, n1, n2, n3))

    print("\n[5] ★ 回归：审计里的撞号场景")
    # 审计复现：先完成剧情任务 1005 发奖（新建背包格子），再完成 1003
    st2 = fresh("ids_b")
    story.start_story_task(st2, 1005, 0)
    story.start_story_task(st2, 1003, 0)
    st2.save()
    story.finish_story_task(st2, 1005, 0)
    story.finish_story_task(st2, 1003, 0)
    st2.save()
    ok, dup = ids_mod.verify_unique(st2.props)
    check("完成两个任务后全 props 无重复 __id__", ok is True, dup)
    check("背包格子 id 各不相同",
          len(bag_ids(st2)) == len(set(bag_ids(st2))), bag_ids(st2))

    print("\n[6] 脚手架入口也共用")
    st3 = fresh("ids_c")
    scaffold.apply(st3)
    ok3, dup3 = ids_mod.verify_unique(st3.props)
    check("脚手架种完数据后无重复 __id__", ok3 is True, dup3)

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

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
