#!/usr/bin/env python
"""P1 阶段 N-c：头衔两个 RPC（RequestDoneTitleTask_CS / RequestRefreshTitleDailyTask_CS）。

★ 客户端契约
  领奖：node_challenge_task_itemgroup.lua:147-171
        只把 state == WaitDone(4) 的 task_bag[10] 任务发进 title_tasks；
        fakeTask 存在时 titel_vit=1，否则 -1。
  刷新：node_daily_task_itemgroup.lua:200-206，参数是 TaskData.DailyType（1/2）。

用法: python tests/test_task_title_rpc.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_title_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
    tasks,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
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


def call(rpc_name, args, rid, uid):
    return guarded_handle(rpc_index.RPC_INDEX[rpc_name], msmod.mp_arr(args),
                          rid, uid=uid)


def _slot_tasks(st, tt):
    return [t for t in ((st.props.get("task_bag") or {}).get(tt) or {})
            if isinstance(t, int)]


def _item_count(st, iid):
    bag = st.props.get("item_bag") or {}
    n = 0
    for v in bag.values():
        if isinstance(v, dict) and v.get("id") == iid:
            n += int(v.get("count") or v.get("num") or 0)
    return n


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 播种后 task_bag[10] 有任务（N-b 顺带完成）")
    st1 = fresh("title")
    t10 = _slot_tasks(st1, 10)
    check("★ task_bag[10] 非空", len(t10) > 0, len(t10))
    check("★ task_bag[9] 非空", len(_slot_tasks(st1, 9)) > 0,
          len(_slot_tasks(st1, 9)))

    print("\n[2] 有奖励的挑战任务：WaitDone -> 领奖 -> Finished + 发奖")
    cand = None
    for t in sorted(t10):
        if (gamedata.task(t) or {}).get("AwardItemList"):
            cand = t
            break
    check("找到有奖励的 type10 任务", cand is not None, cand)
    if cand:
        gamedata.task(cand) or {}
        ch = tasks.move_to_wait_done(st1, cand)
        check("move_to_wait_done 后状态 = WaitDone(4)",
              tasks.task_state(st1.props, cand) == tasks.TASK_STATE_WAIT_DONE,
              tasks.task_state(st1.props, cand))
        check("产生了一条同步描述", len(ch) == 1, len(ch))
        pre = pre_snapshot(uid_of("title"))
        frames = call("RequestDoneTitleTask_CS", [[cand], -1], 1, uid_of("title"))
        check("★ 领奖后状态 = Finished(6)",
              tasks.task_state(st1.props, cand) == tasks.TASK_STATE_FINISHED,
              tasks.task_state(st1.props, cand))
        _done10 = [k for k in ((st1.props.get("done_tasks") or {}).get(10) or {})
                   if isinstance(k, int)]
        check("★ 进了 done_tasks[10]", cand in _done10, _done10[:5])
        sc, errs = frames_safe(pre, frames, "title_claim")
        check("领奖帧过客户端影子缓存", not errs and not sc.warnings,
              (errs[:1], sc.warnings[:1]))
        # 幂等：再领一次不重复发奖
        call("RequestDoneTitleTask_CS", [[cand], -1], 2, uid_of("title"))
        check("★ 重复领奖不报错、状态不变",
              tasks.task_state(st1.props, cand) == tasks.TASK_STATE_FINISHED)

    print("\n[3] Doing 状态的任务不能领奖")
    doing = None
    for t in sorted(t10):
        if t != cand and tasks.task_state(st1.props, t) == tasks.TASK_STATE_DOING:
            doing = t
            break
    check("找到 Doing 状态的挑战任务", doing is not None, doing)
    if doing:
        call("RequestDoneTitleTask_CS", [[doing], -1], 3, uid_of("title"))
        check("★ Doing 领奖被拒（状态不变）",
              tasks.task_state(st1.props, doing) == tasks.TASK_STATE_DOING,
              tasks.task_state(st1.props, doing))

    print("\n[4] 非 type10 任务伪造领奖被拒")
    st4 = fresh("forge")
    t1 = None
    for t in _slot_tasks(st4, 1):
        if tasks.task_state(st4.props, t) == tasks.TASK_STATE_DOING:
            t1 = t
            break
    check("找到 type1 的 Doing 任务", t1 is not None, t1)
    if t1:
        tasks.move_to_wait_done(st4, t1)
        call("RequestDoneTitleTask_CS", [[t1], -1], 4, uid_of("forge"))
        check("★ 非 type10 任务不会被领奖完成",
              tasks.task_state(st4.props, t1) == tasks.TASK_STATE_WAIT_DONE,
              tasks.task_state(st4.props, t1))

    print("\n[5] 刷新头衔日常（按 DailyType）")
    st5 = fresh("refresh")
    daily1 = [t for t in _slot_tasks(st5, 9)
              if int((gamedata.task(t) or {}).get("DailyType") or -1) == 1]
    check("数据里有 DailyType=1 的日常任务", len(daily1) > 0, daily1)
    # 先把一个 DailyType=1 的任务完成掉，再刷新，应回到包里
    if daily1:
        victim = daily1[0]
        tasks.finish_task(st5, victim)
        check("先把它完成（出包）",
              tasks.task_state(st5.props, victim) == tasks.TASK_STATE_FINISHED,
              tasks.task_state(st5.props, victim))
        pre5 = pre_snapshot(uid_of("refresh"))
        frames5 = call("RequestRefreshTitleDailyTask_CS", [1], 5, uid_of("refresh"))
        check("★ 刷新后该任务重新进包且 Doing",
              tasks.task_state(st5.props, victim) == tasks.TASK_STATE_DOING,
              tasks.task_state(st5.props, victim))
        sc5, errs5 = frames_safe(pre5, frames5, "title_refresh")
        check("刷新帧过客户端影子缓存", not errs5 and not sc5.warnings,
              (errs5[:1], sc5.warnings[:1]))

    print("\n[6] 刷新不存在的 DailyType 不报错")
    frames6 = call("RequestRefreshTitleDailyTask_CS", [99], 6, uid_of("refresh"))
    check("DailyType=99 刷新不崩", isinstance(frames6, list) and len(frames6) > 0,
          len(frames6) if isinstance(frames6, list) else frames6)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())