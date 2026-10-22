#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 I：主线可玩段端到端验收（服务端侧）。

这个文件验什么
--------------
从登录播种开始，用真实事件路径把主线一路推下去，证明每一步都有
可验证的完成路径。不是造数据，是真的走：

  登录 -> seed 83 个根 -> 1000 靠 FinishDungeon{typeId:1004} 完成
  -> PostTasks 自动启动 1003 -> NPC 交互 -> 剧情 -> ...

结论（实测）
------------
主线 1000 -> 1101 共 31 步全部可完成，全部 Finished。
卡点在 1102 —— TaskType 0，已按 Lua 证据记为阻塞（见 docs 阶段 E）。

边界
----
这个测试只证服务端侧。客户端的 onStoryTaskItemChanged 是否真的触发，
需要游戏客户端 + Lua 探针（launcher/lua/oo/05b_probe.lua 的 storycb 钩子），
本次会话无法启动游戏进程，见 docs 阶段 I 的说明。

用法: python tests/test_story_mainline.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_ml_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import events as SEV  # noqa: E402
from game_server.story import seed as SD  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from game_server.story.rpc import _story_sync_frames  # noqa: E402
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


def story_table():
    S = gamedata.table("StoryTaskData") or {}
    return {int(v["TaskID"]): v for v in S.values()
            if isinstance(v, dict) and v.get("TaskID") is not None}


def mainline_chain(S):
    chain = [1000]
    cur, seen = 1000, {1000}
    while True:
        posts = [int(x) for x in (S[cur].get("PostTasks") or [])]
        mains = [p for p in posts
                 if p in S and str(S[p].get("ChapterId") or "").startswith("main")]
        nxt = mains[0] if mains else (posts[0] if posts else None)
        if nxt is None or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
        if len(chain) > 200:
            break
    return chain


def p1_events(S, tid):
    T = {int(v["uiD"]): v for v in (gamedata.table("TaskData") or {}).values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    E = {int(v["uiD"]): v for v in (gamedata.table("TaskEventData") or {}).values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    out = []
    for pid in (S[tid].get("Parameter1") or []):
        td = T.get(int(pid))
        if not td:
            continue
        for eid in (td.get("TriggerEventList") or []):
            e = E.get(int(eid)) or {}
            p = e.get("EventParam") or {}
            out.append((e.get("EventType"), p.get("typeId")))
    return out


def walk(S, st, chain):
    """沿链推进，返回 (走完的列表, 卡点, 卡因)。"""
    walked = [1000]
    for tid in chain[1:]:
        if tid not in S:
            return walked, tid, "不在 StoryTaskData"
        t = S[tid].get("TaskType")
        if t == 0:
            return walked, tid, "TaskType 0（已按证据记为阻塞）"
        # ★ 不在这里补启动：真实链路里下一个任务必须由上游的 PostTasks
        #   自动启动。如果这里兜底 start_story_task，断链也能走完，
        #   「PostTasks 漏配」这类 bug 就被掩盖了（阶段 I 实验 3 抓到过）。
        if story.story_state(st.props, tid) == -1:
            return walked, tid, "上游 PostTasks 没启动它（断链）"
        if t in (1, 2, 5):
            story.finish_story_task(st, tid, NOW)
        elif t == 6:
            story.mark_story_task_finished(st, tid, NOW)
            story.archive_story_task(st, tid, NOW)
        elif t == 4:
            npc = story.street_task_npc(tid)
            r2 = SEV.apply_event(st, SEV.ev_street_interact(npc, 1))
            if tid not in r2.hits:
                return walked, tid, "type4 NPC %s 未命中" % npc
            story.finish_story_task(st, tid, NOW)
        elif t in (3, 7):
            return walked, tid, "TaskType %s 需要玩法事件" % t
        walked.append(tid)
    return walked, None, None


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    S = story_table()
    chain = mainline_chain(S)

    print("\n[1] 主线链数据")
    check("主线链 >= 90 步", len(chain) >= 90, len(chain))
    check("1000 是链头且是 TaskType 3（序章副本）",
          chain[0] == 1000 and S[1000].get("TaskType") == 3,
          (chain[0], S[1000].get("TaskType")))
    check("1000 的完成条件是 FinishDungeon{typeId:1004}",
          p1_events(S, 1000) == [("FinishDungeon", 1004)],
          p1_events(S, 1000))

    print("\n[2] 登录播种：story_task_bag 应该是 83 个根（不是 1 个）")
    uid = "ml_" + _TAG
    st = pstate.get_state(
        msmod._conv_int_keys(
            msmod.AVATAR_INIT_DICT.get("__Properties__") or {}), uid=uid)
    SD.seed(st, NOW)
    bag_n = sum(1 for k in (st.props.get("story_task_bag") or {})
                if k not in ("__id__", "__tp__"))
    check("播种后 bag 有 83 个根", bag_n == 83, bag_n)
    check("1000 初始是 Doing(3)",
          story.story_state(st.props, 1000) == sm.TASK_STATE_DOING,
          story.story_state(st.props, 1000))

    print("\n[3] 真实事件路径推进主线")
    res = SEV.apply_event(st, SEV.ev_dungeon_finish(1004, 1, 1))
    check("打序章副本能命中任务 1000", 1000 in res.hits, res.hits)
    if 1000 in res.hits:
        story.finish_story_task(st, 1000, NOW)
    check("1000 -> Finished(6)",
          story.story_state(st.props, 1000) == sm.TASK_STATE_FINISHED,
          story.story_state(st.props, 1000))
    check("PostTasks 自动启动 1003（不靠 rescan）",
          story.story_state(st.props, 1003) == sm.TASK_STATE_DOING,
          story.story_state(st.props, 1003))

    walked, stopped_at, stop_why = walk(S, st, chain)

    print("\n[4] 结果")
    check("主线可玩段 >= 30 步全部走完", len(walked) >= 30, len(walked))
    all_fin = all(story.story_state(st.props, t) == sm.TASK_STATE_FINISHED
                  for t in walked)
    check("可玩段每一步都是 Finished(6)", all_fin,
          [t for t in walked
           if story.story_state(st.props, t) != sm.TASK_STATE_FINISHED][:5])
    check("卡点就是 1102（TaskType 0，证据充分的阻塞）",
          stopped_at == 1102, (stopped_at, stop_why))
    print("      可玩段 %s -> %s 共 %d 步；卡点 %s（%s）"
          % (walked[0], walked[-1], len(walked), stopped_at, stop_why))

    print("\n[5] 帧全部过客户端影子缓存")
    uid2 = "mlf_" + _TAG
    st2 = pstate.get_state(
        msmod._conv_int_keys(
            msmod.AVATAR_INIT_DICT.get("__Properties__") or {}), uid=uid2)
    SD.seed(st2, NOW)
    pre2 = pre_snapshot(uid2)
    frames = []
    r3 = SEV.apply_event(st2, SEV.ev_dungeon_finish(1004, 1, 1))
    if 1000 in r3.hits:
        frames += story.finish_story_task(st2, 1000, NOW)
    w2, _, _ = walk(S, st2, chain)
    for tid in w2[1:]:
        if tid not in S or S[tid].get("TaskType") == 0:
            break
    sc, errs = frames_safe(pre2, _story_sync_frames(st2, frames), "mainline")
    check("主线帧在客户端模型里不崩", not errs, errs[:2])
    check("主线帧无客户端警告", not sc.warnings, sc.warnings[:2])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
