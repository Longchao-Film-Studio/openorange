#!/usr/bin/env python
"""H4：任务事件补算 —— 事件先发生、任务后播种时也要能完成。

实机根因（2026-10-27）：任务 1「完成序章」的触发事件
`FinishDungeon{typeId:1004,modeId:1,level:1}` 在 02:07 发生，
但任务 1 有 PreTask=10（完成取名），直到 02:11 才被播种 -> 事件已过，永远卡在 DOING。
后果：AllSystemCatalogueData 里 39 条 unlockType=1 的系统入口数据条件永假，
街边菜单里的 8 个入口（背包/图鉴/教学/地图/任务/客服/攻略社区/游戏工具）不出现。

修复：_oo_task_event_hits 事件账本（向前）+ 状态推导补算（老存档）+ seed 末尾补算。

用法: python tests/test_task_event_catchup.py
"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_catchup_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (  # noqa: E402
    gamedata,
    rpc_index,  # noqa: E402
    tasks,
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.story import events as events_mod  # noqa: E402
from game_server.story.state_machine import TASK_STATE_DOING, TASK_STATE_FINISHED  # noqa: E402

MENU_SYSTEMS = [7, 23, 48, 11, 3, 52, 74, 75]   # SystemMenuData 里任务 1 门槛的 8 个入口

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def boot(uid):
    initdict.live_init_dict(uid)
    return pstate.get_state(uid=uid)


def bare(uid):
    props = copy.deepcopy(initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"]))
    return pstate.PlayerState(uid=uid, props=props)


def unlocked(props, sid):
    us = (props.get("system_p") or {}).get("unlocked_system") or {}
    return us.get(sid) is True


def frame_names(frames):
    out = []
    for f in frames:
        if not isinstance(f, (bytes, bytearray)) or len(f) < 6:
            continue
        if int.from_bytes(f[4:6], "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        try:
            fields = msmod.pb_parse(f[6:])
            if 4 in fields:
                out.append(fields[4][0].decode("utf-8", "replace"))
        except Exception:
            continue
    return out


def run():
    print("\n[1] 事件账本：事件发生时任务还不在包，也要记下来")
    st = bare("c1")
    check("任务 1 一开始不在包", tasks.task_state(st.props, 1) == -1)
    n = tasks.record_event_hits(st, "FinishDungeon",
                                {"typeId": 1004, "modeId": 1, "level": 1})
    hits = st.props.get(tasks.EVENT_HITS_KEY) or {}
    check("记录了命中（4001）", n >= 1 and hits.get("4001", 0) >= 1, hits)
    tasks.record_event_hits(st, "FinishDungeon",
                            {"typeId": 9999, "modeId": 1, "level": 1})
    hits2 = st.props.get(tasks.EVENT_HITS_KEY) or {}
    check("不匹配的事件不记账", hits2.get("4001") == 1, hits2)

    print("\n[2] 账本补算：先有事件、后播种任务 -> 任务直接完成")
    st = bare("c2")
    done = st.props["done_tasks"]
    done.setdefault(1, {})[10] = 1          # PreTask 10 已完成（取名）
    events_mod.apply_event(st, events_mod.ev_dungeon_finish(1004, 1, 1))
    check("事件后任务 1 仍不在包", tasks.task_state(st.props, 1) == -1)
    ch = tasks.seed_tasks(st)
    check("播种+补算后任务 1 已完成",
          tasks.task_state(st.props, 1) == TASK_STATE_FINISHED,
          tasks.task_state(st.props, 1))
    check("产生了任务同步描述", bool(ch), len(ch))
    from game_server.story.rpc import recalc_system_unlocks
    frames = recalc_system_unlocks(st)
    check("系统 7（背包）已解锁", unlocked(st.props, 7), frames[:2])
    check("8 个菜单入口全部解锁", all(unlocked(st.props, s) for s in MENU_SYSTEMS),
          {s: unlocked(st.props, s) for s in MENU_SYSTEMS})

    print("\n[3] 状态推导：老存档没有账本，靠 done_story_tasks 反推")
    st = bare("c3")
    done = st.props["done_tasks"]
    done.setdefault(1, {})[10] = 1
    # StoryTask 1000 = TaskType 3, Parameter1=[1] -> TaskData[1].TriggerEventList=[4001]
    st.props["done_story_tasks"][1000] = {"__id__": 1, "id": 1000, "state": 6}
    deriv = tasks.derivable_event_hits(st)
    check("从剧情完成状态推导出 4001", deriv.get("4001") == 1, deriv)
    tasks.seed_tasks(st)
    check("补算后任务 1 已完成",
          tasks.task_state(st.props, 1) == TASK_STATE_FINISHED,
          tasks.task_state(st.props, 1))
    recalc_system_unlocks(st)
    check("系统 7 已解锁（老存档自愈）", unlocked(st.props, 7))

    print("\n[4] 多档 TriggerTimes：次数不足不完成，够了才完成")
    multi = None
    for tid in tasks.all_task_ids():
        cfg = gamedata.task(tid) or {}
        evs = cfg.get("TriggerEventList") or []
        times = cfg.get("TriggerTimes") or [1]
        if len(evs) == 1 and int(times[0]) >= 3 and not cfg.get("PreTask"):
            multi = (tid, int(evs[0]), int(times[0]))
            break
    check("找到多档任务样本", multi is not None, multi)
    def _set_hits(st, mapping):
        st.props[tasks.EVENT_HITS_KEY] = dict(mapping)

    if multi:
        tid, eid, need = multi
        st = bare("c4")
        tasks.start_task(st, tid, 1793000000)
        _set_hits(st, {str(eid): need - 1})
        tasks.catch_up_task_events(st)
        check("次数不足仍是 DOING",
              tasks.task_state(st.props, tid) == TASK_STATE_DOING,
              tasks.task_state(st.props, tid))
        _set_hits(st, {str(eid): need})
        tasks.catch_up_task_events(st)
        check("次数够了才完成",
              tasks.task_state(st.props, tid) == TASK_STATE_FINISHED,
              tasks.task_state(st.props, tid))

    print("\n[5] 新号同一会话：取名 -> 任务 1 立即播种 -> 序章事件当场完成")
    st = boot("c5")
    from game_server import dispatch
    frames = dispatch.handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                             msmod.mp_arr(["测试导演", 1]), 1, uid="c5")
    check("取名成功", bool(frames), len(frames))
    check("任务 10 已完成", tasks.task_state(st.props, 10) == TASK_STATE_FINISHED,
          tasks.task_state(st.props, 10))
    check("任务 1 已当场播种（DOING）",
          tasks.task_state(st.props, 1) == TASK_STATE_DOING,
          tasks.task_state(st.props, 1))
    res = events_mod.apply_event(st, events_mod.ev_dungeon_finish(1004, 1, 1))
    check("序章事件后任务 1 完成",
          tasks.task_state(st.props, 1) == TASK_STATE_FINISHED,
          tasks.task_state(st.props, 1))
    check("当场解锁系统 7（帧里带 unlocked_system）",
          unlocked(st.props, 7), frame_names(res.frames)[:5])

    print("\n[6] 幂等：重复播种/补算不重复完成、不重复推帧")
    st = bare("c6")
    st.props["done_tasks"].setdefault(1, {})[10] = 1
    events_mod.apply_event(st, events_mod.ev_dungeon_finish(1004, 1, 1))
    tasks.seed_tasks(st)
    first = tasks.task_state(st.props, 1)
    ch2 = tasks.seed_tasks(st)
    check("第二次 seed 没有任务变更", not [c for c in ch2 if c and c[0] == "sync" and c[2] == tasks.OP_ADD], len(ch2))
    check("任务 1 仍是 FINISHED 且只完成一次",
          first == TASK_STATE_FINISHED and tasks.task_state(st.props, 1) == TASK_STATE_FINISHED,
          (first, tasks.task_state(st.props, 1)))
    check("done_tasks 没被重复写坏",
          isinstance(st.props["done_tasks"].get(1, {}).get(1), int),
          st.props["done_tasks"].get(1, {}).get(1))

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
