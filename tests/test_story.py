#!/usr/bin/env python
"""阶段 2：剧情任务推进测试。

逆向依据 entities/avatarcomponent/avatarstorytaskcomp.lua:617-710：
  getStoryTaskState(taskID):
    story_task_bag[taskID].state，或 done_story_tasks[taskID] 存在 -> Finished(6)
  getTaskIdByParamInt(param_int, type):
    遍历 story_task_bag，TaskType == type 且 Parameter1 含 param_int

推进链：StoryTaskData[taskId].PostTasks 决定下一个任务。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_story_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, gamedata, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []

def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))

def run():
    print("\n[1] 索引与处理器")
    for n in ("RequestDoneStoryTaskParamInt_CS", "RequestDoneStoryTaskParamStr_CS",
              "RequestStreetActor_CS"):
        check(n + " 在索引表", n in erpc.RPC_INDEX)
        check(n + " 有处理器", erpc.RPC_INDEX.get(n) in erpc._HANDLERS)

    print("\n[2] StoryTaskData[1000] 的 PostTasks 是推进依据")
    cfg = gamedata.story_task(1000) or {}
    check("1000 存在", bool(cfg))
    posts = cfg.get("PostTasks") or []
    check("1000 有 PostTasks", len(posts) > 0, posts)

    print("\n[3] 把 1000 设为进行中")
    config.configure_scaffold(unlock_all=True)
    msmod._live_init_dict("story_test")
    st = pstate.get_state(uid="story_test")
    now = 1700000000
    changes = story.start_story_task(st, 1000, now)
    check("返回同步项", len(changes) > 0, len(changes))
    check("story_task_bag 里有 1000", 1000 in st.props["story_task_bag"])
    it = st.props["story_task_bag"][1000]
    check("state = Doing(3)", it.get("state") == story.TASK_STATE_DOING, it.get("state"))
    check("__tp__ = StoryTaskItem", it.get("__tp__") == "StoryTaskItem", it.get("__tp__"))
    check("story_state() 读到 3", story.story_state(st.props, 1000) == 3)
    # 嵌套的自定义属性必须带 __id__，否则客户端 init 报
    #   class ClientAvatar function init must have a boolean return value
    for f in ("doing_targets", "finish_targets", "finish_parameter"):
        check(f"嵌套字段 {f} 带 __id__",
              isinstance((it.get(f) or {}).get("__id__"), int), it.get(f))

    print("\n[4] 通过事件完成 1000（TaskType=3 是事件驱动，客户端不会为它发 RPC）")
    # ★ P1 阶段 B 起，RequestDoneStoryTaskParamInt_CS 只接受 TaskType 1/2/5（防伪造），
    #   1000 是 TaskType 3，必须走真实的事件路径（和 dungeon/settle.py 一样）。
    from game_server.story import events as _sev
    _res = _sev.apply_event(st, _sev.ev_dungeon_finish(1004, 1, 1))
    frames = erpc._finish_story_tasks(st, list(_res.hits))
    check("返回多帧", len(frames) >= 3, len(frames))
    check("done_story_tasks 里有 1000", 1000 in st.props["done_story_tasks"])
    check("story_task_bag 里没有 1000", 1000 not in st.props["story_task_bag"])
    check("story_state(1000) = Finished(6)",
          story.story_state(st.props, 1000) == story.TASK_STATE_FINISHED)

    print("\n[5] 自动推进到 PostTasks 的第一个任务")
    nxt = int((cfg.get("PostTasks") or [0])[0])
    check(f'story_task_bag 里有下一个任务 {nxt}',
          nxt in st.props["story_task_bag"], list(st.props["story_task_bag"].keys())[:5])
    check("下一个任务是 Doing(3)",
          st.props["story_task_bag"].get(nxt, {}).get("state") == 3)

    print("\n[6] 发奖励：AwardItemList")
    awards = cfg.get("AwardItemList") or {}
    ids = [int(k) for k in awards if k not in ("KeyList", "__bdd_metatype__")]
    bag = st.props.get("item_bag") or {}
    have = {g.get("itemId") for g in bag.values() if isinstance(g, dict)}
    if ids:
        check(f"奖励道具 {ids} 进了背包", all(i in have for i in ids), sorted(have))
    else:
        check("1000 无奖励（跳过）", True)

    print("\n[7] 重复完成不重复推进")
    before = dict(st.props["story_task_bag"])
    guarded_handle(erpc.RPC_INDEX["RequestDoneStoryTaskParamInt_CS"],
                msmod.mp_arr([1000, 0]), 2, uid="story_test")
    check("story_task_bag 没变", set(before.keys()) == set(st.props["story_task_bag"].keys()))

    print("\n[8] 街景 NPC 交互 = 完成 Parameter1 命中的任务")
    # 找一个 TaskType=2 且 Parameter1 含某 actor 的剧情任务
    target = None
    for tid in (1003, 1032, 1042, 1048, 1004, 1007):
        c = gamedata.story_task(tid) or {}
        p1 = c.get("Parameter1") or []
        if c.get("TaskType") == 2 and p1:
            target = (tid, p1[0])
            break
    check("找到 TaskType=2 的样例任务", target is not None, target)
    if target:
        tid, actor = target
        story.start_story_task(st, tid, now)
        frames = guarded_handle(erpc.RPC_INDEX["RequestStreetActor_CS"],
                             msmod.mp_arr([actor]), 3, uid="story_test")
        check(f'命中并完成 {tid}',
              story.story_state(st.props, tid) == story.TASK_STATE_FINISHED,
              story.story_state(st.props, tid))

    print("\n[9] 系统解锁按剧情重算")
    sp = st.props.get("system_p") or {}
    us = sp.get("unlocked_system") or {}
    # ★ 2026-10-22 修复空断言：原来是 check(..., True)，条件写死永远不红。
    #   改成「从数据表核实 2/4/55 确实是 unlockType=2 且 unlockParam=1005」。
    #   这条假阳性是 docs/检讨-功能解锁遗漏.md 里点名的那一条。
    from game_server import gamedata as _gd
    _rows = {r.get("id"): r for r in _gd.all_system_catalogue()
             if isinstance(r, dict)}
    _want = sorted(sid for sid, r in _rows.items()
                   if r.get("unlockType") == 2 and r.get("unlockParam") == 1005)
    check("数据里 unlockType=2 且 unlockParam=1005 的入口是 [2, 4, 55]",
          _want == [2, 4, 55], _want)
    story.start_story_task(st, 1005, now)
    guarded_handle(erpc.RPC_INDEX["RequestDoneStoryTaskParamInt_CS"],
                msmod.mp_arr([1005, 0]), 4, uid="story_test")
    check("系统 2 已解锁", us.get(2) is True, us.get(2))
    check("系统 4 已解锁", us.get(4) is True, us.get(4))
    check("系统 55 已解锁", us.get(55) is True, us.get(55))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())