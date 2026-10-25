#!/usr/bin/env python
"""脚手架覆盖层测试（game_server/scaffold.py）。

架构：脚手架是**运行时可开关的覆盖层**，不烤进 avatar_init.json。
  关闭（默认）-> 走和真实游戏一致的开局
  开启（--unlock-all）-> 全系统解锁 + 任务全完成 + 等级拉高

为什么这么设计：烤进 JSON 就再也关不掉，而且会掩盖服务端真正的 bug
（分不清功能能用是因为服务端对了，还是被强行解锁了）。
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, scaffold
from game_server import ms as msmod

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def base_props():
    return msmod._conv_int_keys(copy.deepcopy(msmod.AVATAR_INIT_DICT["__Properties__"]))


def run():
    print("\n[1] 默认关闭：什么都不改")
    cfg = config.Scaffold()
    check("默认 unlock_all=False", cfg.unlock_all is False)
    check("默认 level=None", cfg.level is None)
    p = base_props()
    before_level = p.get("level")
    changed = scaffold.apply(p, cfg)
    check("没有改动", changed == [], changed)
    check("level 没变", p.get("level") == before_level, p.get("level"))

    print("\n[2] unlock_all：系统全部解锁")
    cfg2 = config.Scaffold(unlock_all=True)
    p2 = base_props()
    changed2 = scaffold.apply(p2, cfg2)
    us = p2["system_p"]["unlocked_system"]
    int_keys = [k for k in us if isinstance(k, int)]
    check("解锁条目数 = 常量表长度", len(int_keys) == len(scaffold.SYSTEM_UNLOCKS),
          (len(int_keys), len(scaffold.SYSTEM_UNLOCKS)))
    for sid in (1, 4, 7, 11, 30, 1001):
        check('系统 {} 已解锁'.format(sid), us.get(sid) is True, us.get(sid))
    check("改动里报告了解锁", any("系统解锁" in c for c in changed2), changed2)

    print("\n[3] all_tasks_done：任务数据形状正确")
    # 三个坑（都踩过）：
    #   done_tasks 的值类型是 IntDictKeyInt（不是 DoneTaskTypeBag）
    #   done_story_tasks 的值类型是 StoryTaskItem 对象（不能直接塞 true）
    #   getSystemTaskState 会先看 task_bag[taskType] 是否存在
    tb = p2.get("task_bag") or {}
    slot = tb.get(1)
    check("task_bag 有 TaskType=1 槽位", isinstance(slot, dict), type(slot))
    check("task_bag 槽位带 __id__", isinstance((slot or {}).get("__id__"), int),
          (slot or {}).get("__id__"))

    dt = p2.get("done_tasks") or {}
    dt1 = dt.get(1) or {}
    check("done_tasks[1] 是 dict", isinstance(dt1, dict), type(dt1))
    check("done_tasks[1] 带 __id__", isinstance(dt1.get("__id__"), int), dt1.get("__id__"))
    for tid in scaffold.DONE_TASK_IDS:
        check('done_tasks[1][{}] 为真'.format(tid), bool(dt1.get(tid)), dt1.get(tid))

    dst = p2.get("done_story_tasks") or {}
    item = dst.get(1005) or {}
    check("done_story_tasks[1005] 是对象不是 true", isinstance(item, dict), type(item))
    check("  .id = 1005", item.get("id") == 1005, item.get("id"))
    check("  .state = 6 (Finished)", item.get("state") == 6, item.get("state"))
    check("  .doing_targets 是带 __id__ 的 dict",
          isinstance((item.get("doing_targets") or {}).get("__id__"), int),
          item.get("doing_targets"))
    check("13 条剧情任务都写了",
          len([k for k in dst if isinstance(k, int)]) == len(scaffold.DONE_STORY_TASK_IDS),
          len([k for k in dst if isinstance(k, int)]))

    print("\n[4] __id__ 不冲突")
    def all_ids(obj, out):
        if isinstance(obj, dict):
            v = obj.get("__id__")
            if isinstance(v, int):
                out.append(v)
            for v2 in obj.values():
                all_ids(v2, out)
        elif isinstance(obj, list):
            for v in obj:
                all_ids(v, out)
    ids = []
    all_ids(p2, ids)
    dup = len(ids) - len(set(ids))
    check("没有重复 __id__", dup == 0, '重复 {} 个'.format(dup))

    print("\n[5] level 覆盖")
    cfg3 = config.Scaffold(level=42)
    p3 = base_props()
    scaffold.apply(p3, cfg3)
    check("level = 42", p3.get("level") == 42, p3.get("level"))

    print("\n[6] 描述文本")
    check("关闭时描述正确", "关闭" in config.Scaffold().describe(),
          config.Scaffold().describe())
    check("开启时描述正确", "全系统解锁" in config.Scaffold(unlock_all=True).describe(),
          config.Scaffold(unlock_all=True).describe())

    print("\n[7] 幂等：重复 apply 不重复加条目")
    p4 = base_props()
    scaffold.apply(p4, cfg2)
    n1 = len([k for k in p4["done_story_tasks"] if isinstance(k, int)])
    scaffold.apply(p4, cfg2)
    n2 = len([k for k in p4["done_story_tasks"] if isinstance(k, int)])
    check("剧情任务条数不变", n1 == n2, (n1, n2))

    print("\n[8] 嵌套自定义值不能带 __tp__（会打断客户端 initProperty）")
    # 2026-10-20 实机踩过：task_bag[type] 是 TaskBag（嵌套容器），
    # 给它加 __tp__ 后客户端解析 CustomDict 走错分支，
    # initProperty 提前中断 -> item_bag 没建 -> AvatarBagComp:refreshLocalBagData
    # 里 pairs(nil) 报错 -> ClientAvatar:init 返回非布尔 -> 进游戏被踢回登录页。
    p3 = base_props()
    scaffold.apply(p3, config.Scaffold(unlock_all=True))
    tb = p3.get("task_bag") or {}
    for tt in [k for k in tb if isinstance(k, int)]:
        slot = tb.get(tt) or {}
        check('task_bag[{}] 有 __id__'.format(tt), isinstance(slot.get("__id__"), int), slot)
        check('task_bag[{}] 没有 __tp__'.format(tt), "__tp__" not in slot, slot.get("__tp__"))

    print("\n[9] 脚手架要把 story_finished[1] 置真（跳过序章）")
    # 2026-10-21：脚手架模式下 ms.py:464 会跳过 init_new_player，
    # 但 story_finished 若仍为空，客户端照样进序章，而序章里没有活动剧情任务，
    # 形成「进得去但打完什么也不推进」的怪状态。
    # 脚手架语义 = 老号全解锁，所以直接标序章已完成。
    p4 = base_props()
    scaffold.apply(p4, config.Scaffold(unlock_all=True))
    sf = p4.get("story_finished") or {}
    check("story_finished 有整数键 1", 1 in sf, sorted([k for k in sf if isinstance(k, int)]))
    check("story_finished[1] = True", sf.get(1) is True, sf.get(1))

    # 默认（无脚手架）不能动它，否则新号看不到序章
    p5 = base_props()
    scaffold.apply(p5, config.Scaffold())
    sf5 = p5.get("story_finished") or {}
    check("无脚手架时 story_finished 仍为空",
          not [k for k in sf5 if isinstance(k, int)],
          sorted([k for k in sf5 if isinstance(k, int)]))

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
