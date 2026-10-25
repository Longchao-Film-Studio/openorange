#!/usr/bin/env python
"""P1 阶段 D：链式扫全表（rescan）。

★ 背景：阶段 C 实测发现 PostTasks 与 PreTask 字段 958/958 完全一致，
  所以「线性推进」覆盖了绝大多数任务。真正漏的只有三类：

    1. 跨表前置（PreTaskType=1，6 条）—— 上游是 TaskData，
       StoryTaskData.PostTasks 不会启动它。
    2. 链式闸门（TaskType 6，24 条）—— events.tick 只在登录和副本结算跑。
    3. 断链（400006 -> 1124 不存在）—— 扫描必须容错。

★ 不变量

  1. rescan 幂等：连续调两次，第二次返回 0 帧。
  2. 断链不崩，且一次性告警。
  3. 悬空 TaskData id（67 个）一次性告警，不混在「卡住」里。
  4. 跨表前置的上游（TaskData）完成后，rescan 能推进那 6 条链。
  5. 不是「扫描 1047 条找下一步」—— 线性推进走 PostTasks 就够。

用法: python tests/test_story_rescan.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_rescan_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import seed as seed_mod  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
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


def clear_story(st):
    for prop in ("story_task_bag", "done_story_tasks"):
        d = st.props.get(prop)
        if isinstance(d, dict):
            for k in [k for k in d if k not in ("__id__", "__tp__")]:
                d.pop(k, None)
    return st


def sync_frames(st, changes):
    from game_server.story.rpc import _story_sync_frames
    return _story_sync_frames(st, changes)


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 数据缺陷检测：断链 / 悬空 TaskData id")
    broken = seed_mod._broken_pre_tasks()
    check("★ 真断链只有 1 条（400006 -> 1124）",
          broken == [(400006, 1124)], broken)
    dangling = seed_mod._dangling_task_data_ids()
    check("★ 悬空 TaskData id 共 67 个", len(dangling) == 67, len(dangling))
    # 跨表前置不算断链
    cross = [t for t in (seed_mod._INDEX or {})
             if (seed_mod.cfg_of(t) or {}).get("PreTaskType") == 1]
    check("跨表前置 6 条不被误判成断链",
          all(t not in [b[0] for b in broken] for t in cross), cross)

    print("\n[2] rescan 不崩 + 幂等")
    st2 = fresh("rs_a")
    pre2 = pre_snapshot(uid_of("rs_a"))
    ch1 = seed_mod.rescan(st2, NOW)
    check("rescan 不抛异常且返回列表", isinstance(ch1, list), type(ch1))
    ch2 = seed_mod.rescan(st2, NOW)
    check("★ 第二次 rescan 返回 0 帧（幂等）", ch2 == [], len(ch2))
    if ch1:
        sc, errs = frames_safe(pre2, sync_frames(st2, ch1), "rescan")
        check("rescan 产生的帧在客户端模型里不崩且无警告",
              not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[3] 跨表前置：TaskData 完成后 rescan 能推进")
    # 找一条跨表前置任务（PreTaskType=1），把它的 TaskData 上游标完成
    cross_id = None
    for t in sorted(seed_mod._INDEX or {}):
        cfg = seed_mod.cfg_of(t) or {}
        if cfg.get("PreTaskType") == 1 and cfg.get("PreTask") is not None:
            cross_id = t
            break
    check("找到跨表前置任务", cross_id is not None, cross_id)
    if cross_id is not None:
        st3 = clear_story(fresh("rs_b"))
        pre3 = pre_snapshot(uid_of("rs_b"))
        # 上游未完成 -> 不播
        ch_before = seed_mod.rescan(st3, NOW)
        check("★ 上游 TaskData 未完成时，跨表任务不被播种",
              cross_id not in [int(c[2]) for c in ch_before if c[0] == "add"],
              [c[2] for c in ch_before if c[0] == "add"][:5])
        # 把上游 TaskData 标完成
        from game_server import tasks as _tasks
        pre_tid = int((seed_mod.cfg_of(cross_id) or {}).get("PreTask"))
        _tasks.start_task(st3, pre_tid, NOW)
        _tasks.finish_task(st3, pre_tid, NOW)
        ch_after = seed_mod.rescan(st3, NOW)
        check("★ 上游 TaskData 完成后，跨表任务被 rescan 播种",
              cross_id in [int(c[2]) for c in ch_after if c[0] == "add"],
              [c[2] for c in ch_after if c[0] == "add"][:5])
        check("★ 播种后状态是 Doing(3)",
              story.story_state(st3.props, cross_id) == sm.TASK_STATE_DOING,
              story.story_state(st3.props, cross_id))

    print("\n[4] 真实链路：RequestDoneTask_CS 完成 TaskData 后剧情任务被推进")
    if cross_id is not None:
        st4 = clear_story(fresh("rs_c"))
        from game_server import tasks as _tasks
        pre_tid = int((seed_mod.cfg_of(cross_id) or {}).get("PreTask"))
        # ★ 快照必须在 start_task **之后**取：否则 finish_task 发的 OP_DEL
        #   在客户端缓存里找不到对应条目（测试假象，不是生产 bug）。
        _tasks.start_task(st4, pre_tid, NOW)
        pre4 = pre_snapshot(uid_of("rs_c"))
        frames = guarded_handle(
            erpc.RPC_INDEX.get("RequestDoneTask_CS") or 1,
            msmod.mp_arr([pre_tid]), 1, uid=uid_of("rs_c"))
        check("★ 走 RequestDoneTask_CS 后跨表剧情任务进包",
              cross_id in st4.props.get("story_task_bag", {}),
              list(st4.props.get("story_task_bag", {}).keys())[:5])
        sc, errs = frames_safe(pre4, frames, "task_done")
        check("★ 这条链路的帧在客户端模型里不崩且无警告",
              not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[5] 链式闸门：TaskType 6 经 rescan 推进")
    gate = 201000
    cfg_g = seed_mod.cfg_of(gate) or {}
    if cfg_g.get("TaskType") == 6:
        st5 = clear_story(fresh("rs_d"))
        story.start_story_task(st5, gate, NOW)
        p1_g = [int(x) for x in (cfg_g.get("Parameter1") or [])]
        for pre in p1_g:
            if story.story_state(st5.props, pre) != -1:
                continue
            story.mark_story_task_finished(st5, pre, NOW)
            story.archive_story_task(st5, pre, NOW)
        ch5 = seed_mod.rescan(st5, NOW)
        check("★ Type6 闸门 %s 前置满足后经 rescan 完成" % gate,
              story.story_state(st5.props, gate) == sm.TASK_STATE_FINISHED,
              story.story_state(st5.props, gate))

    print("\n[6] 断链容错：含断链的全量扫描不崩")
    st6 = clear_story(fresh("rs_e"))
    try:
        ch6 = seed_mod.rescan(st6, NOW)
        check("★ 含断链（400006->1124）的 rescan 不抛异常",
              isinstance(ch6, list), type(ch6))
    except Exception as e:
        check("★ 含断链的 rescan 不抛异常", False, e)
    # 400006 不可达，不该被播种
    check("★ 断链任务 400006 不被播种",
          400006 not in st6.props.get("story_task_bag", {}))

    print("\n[7] rescan 不是「扫描 1047 条找下一步」")
    # 线性推进走 PostTasks：完成 1000 应该自动启动 1003，不需要 rescan
    st7 = clear_story(fresh("rs_f"))
    story.start_story_task(st7, 1000, NOW)
    story.finish_story_task(st7, 1000, NOW)
    check("★ 1000 完成后 1003 由 PostTasks 自动启动（不靠 rescan）",
          1003 in st7.props.get("story_task_bag", {}),
          list(st7.props.get("story_task_bag", {}).keys())[:5])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
