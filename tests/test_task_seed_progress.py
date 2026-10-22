#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 N-a2 / N-a3：通用任务播种 + 事件驱动进度推进。

★ 为什么有这个测试
  之前 task_bag 实测 0 槽位、0 任务，于是：
    getSystemTaskState(任意) 恒 -1
    -> AllSystemCatalogueData 里 40 条 unlockType=1 的系统入口数据条件永假
  本测试锁住「播种 -> 事件推进 -> 完成 -> getSystemTaskState 返回 Finished」这条链。
  见 docs/P1-剧情任务状态机.md 阶段 N 的 N2.1 ④ / ⑦ / ⑧。

用法: python tests/test_task_seed_progress.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_seed_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import tasks  # noqa: E402
from game_server.story import events as sev  # noqa: E402
from game_server.story.tasks import drain_event_task_sync  # noqa: E402
from tests.frame_guard import pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
NOW = 1700000000
_TAG = ""


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return "%s_%s" % (name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def _count(st):
    tb = st.props.get("task_bag") or {}
    return sum(len([x for x in tb[k] if isinstance(x, int)])
               for k in tb if isinstance(k, int))


def _bag_item(st, tid):
    """从 task_bag 里取某任务条目（找不到返回 None）。"""
    cfg = gamedata.task(tid) or {}
    slot = (st.props.get("task_bag") or {}).get(cfg.get("TaskType"))
    if not isinstance(slot, dict):
        return None
    it = slot.get(tid)
    return it if isinstance(it, dict) else None


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 登录即播种（N-a2）")
    st1 = fresh("seed")
    n1 = _count(st1)
    check("★ 登录后 task_bag 非空", n1 > 0, n1)
    slots = sorted(k for k in (st1.props.get("task_bag") or {}) if isinstance(k, int))
    check("★ 播种覆盖多个 TaskType 槽位", len(slots) >= 3, slots)
    check("★ task 10（完成取名）已进包",
          tasks.task_state(st1.props, 10) == tasks.TASK_STATE_DOING,
          tasks.task_state(st1.props, 10))
    check("★ task 1（完成序章）还没进包（PreTask=10 未完成）",
          tasks.task_state(st1.props, 1) == -1,
          tasks.task_state(st1.props, 1))

    print("\n[2] 播种幂等（N-a2）")
    ch2 = tasks.seed_tasks(st1, NOW)
    check("★ 再播一次不新增", len(ch2) == 0 and _count(st1) == n1,
          (len(ch2), _count(st1), n1))

    print("\n[3] PreTask 判定按 PreTaskType 分表（N-a2）")
    check("★ 未完成的 TaskData 前置 -> 不满足（task 1 的 Pre=10）",
          tasks.pre_satisfied(st1, 1) is False)
    check("★ 无 PreTask 的恒满足（task 10）",
          tasks.pre_satisfied(st1, 10) is True)

    print("\n[4] Sign 事件推进 task 10（N-a3）")
    pre4 = pre_snapshot(uid_of("seed"))
    res4 = sev.apply_event(st1, sev.ev_sign())
    _done = st1.props.get("done_tasks") or {}
    _slot1 = _done.get(1) or {}
    check("★ done_tasks[1] 里有 task 10",
          10 in [k for k in _slot1 if isinstance(k, int)],
          [k for k in _slot1 if isinstance(k, int)][:5])
    check("★ task 10 已完成（从 task_bag 出包）",
          tasks.task_state(st1.props, 10) == tasks.TASK_STATE_FINISHED,
          tasks.task_state(st1.props, 10))
    check("★ EventResult 带回了 task_sync", len(res4.task_sync) > 0,
          len(res4.task_sync))
    frames4 = drain_event_task_sync(st1, res4)
    sc4, errs4 = frames_safe(pre4, frames4, "task_progress")
    check("进度帧过客户端影子缓存", not errs4 and not sc4.warnings,
          (errs4[:1], sc4.warnings[:1]))

    print("\n[5] task 10 完成后 task 1 可播（N-a2 链式）")
    ch5 = tasks.seed_tasks(st1, NOW)
    check("★ task 1 已进包且 Doing",
          tasks.task_state(st1.props, 1) == tasks.TASK_STATE_DOING,
          tasks.task_state(st1.props, 1))
    check("★ 这一轮确实播了新任务", len(ch5) > 0, len(ch5))

    print("\n[6] FinishDungeon 完成 task 1 -> 40 条解锁的数据条件成立")
    sev.apply_event(st1, sev.ev_dungeon_finish(1004, 1, 1))
    check("★ task 1 完成",
          tasks.task_state(st1.props, 1) == tasks.TASK_STATE_FINISHED,
          tasks.task_state(st1.props, 1))
    check("★ 关键：getSystemTaskState(1) 等价判定 == Finished（6）",
          tasks.task_state(st1.props, 1) == 6)

    print("\n[7] 事件不匹配不推进")
    st7 = fresh("nomatch")
    it10a = _bag_item(st7, 10)
    before = dict((it10a or {}).get("doing_targets") or {})
    sev.apply_event(st7, sev.ev_dungeon_finish(9999, 1, 1))
    it10b = _bag_item(st7, 10)
    after = dict((it10b or {}).get("doing_targets") or {})
    check("★ 无关事件不会推进 task 10", before == after, (before, after))

    print("\n[8] TriggerTimes>1：进度累加，够了才完成")
    st8 = fresh("multi")
    multi = None
    for tt_slot in sorted(k for k in (st8.props.get("task_bag") or {}) if isinstance(k, int)):
        for t in (st8.props["task_bag"][tt_slot] or {}):
            if not isinstance(t, int):
                continue
            cfg = gamedata.task(t) or {}
            tt = cfg.get("TriggerTimes") or []
            try:
                if int(tt[0]) > 1 and cfg.get("TriggerEventList"):
                    multi = (t, int(tt[0]))
                    break
            except Exception:
                continue
        if multi:
            break
    check("找到 TriggerTimes>1 且已播种的任务", multi is not None, multi)
    if multi:
        tid8, need8 = multi
        cfg8 = gamedata.task(tid8) or {}
        eid8 = int(cfg8["TriggerEventList"][0])
        ev8 = gamedata.task_event(eid8) or {}
        etype8 = ev8.get("EventType")
        ep8 = {k: v for k, v in (ev8.get("EventParam") or {}).items()
               if k not in ("KeyList", "__bdd_metatype__")}
        d1, _s1 = tasks.evaluate_event(st8, etype8, ep8)
        it8 = _bag_item(st8, tid8)
        dt8 = (it8 or {}).get("doing_targets") or {}
        check("★ 喂 1 次后 doing_targets 记到 1",
              int(dt8.get(eid8) or 0) == 1, dict(dt8))
        check("★ 喂 1 次后还没完成（need=%d）" % need8, tid8 not in d1, d1)
        done8 = []
        for _ in range(need8 - 1):
            done8, _s2 = tasks.evaluate_event(st8, etype8, ep8)
        check("★ 喂够 %d 次后达到完成条件" % need8, tid8 in done8, done8)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())