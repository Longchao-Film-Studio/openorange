#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 E：TaskType 0 / TaskType 4。

★ Type4（13 条）—— 有证据，已实现
  完成信号 = StreetInteract 事件带 GuideParam 里的 NPC actorId。
  依据：13 条全部有 Parameter1(StreetData 键) + GuideParam；
        GuideParam 全部命中 StreetActorInfoData.actorId；
        StreetActorInfoData.preTask = {taskId: 需要的状态}（NPC 要任务在做才出现）。

★ Type0（270 条）—— 无证据，明确标为阻塞
  0/270 有 Parameter1；客户端对 type0 的完成上报调用点为 0；
  TaskBgStory 只是显示文本；256/270 的 dgareaId -> MainlineData -> dungeonid，
  但主线尾段 1102->11091 有 11 条连续 type0 共用同一个 dgareaId，
  不可能每条都要求打一次副本。按 R7 不猜，记为阻塞并给出证据。

用法: python tests/test_story_type04.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_t04_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import events as sev  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from game_server.story.rpc import _finish_story_tasks  # noqa: E402
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


def clear_story(st):
    for prop in ("story_task_bag", "done_story_tasks"):
        d = st.props.get(prop)
        if isinstance(d, dict):
            for k in [k for k in d if k not in ("__id__", "__tp__")]:
                d.pop(k, None)
    return st


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    S = gamedata.table("StoryTaskData") or {}
    S = {int(v["TaskID"]): v for v in S.values()
         if isinstance(v, dict) and v.get("TaskID") is not None}
    t4 = sorted(t for t, v in S.items() if v.get("TaskType") == 4)
    t0 = sorted(t for t, v in S.items() if v.get("TaskType") == 0)

    print("\n[1] 数据事实：type4 共 %d 条，type0 共 %d 条" % (len(t4), len(t0)))
    check("type4 共 13 条", len(t4) == 13, len(t4))
    check("type0 共 270 条", len(t0) == 270, len(t0))
    check("★ 13 条 type4 全部有 GuideParam",
          all(S[t].get("GuideParam") for t in t4))
    check("★ 13 条 type4 的 GuideParam 全部能解析出 NPC",
          all(story.street_task_npc(t) is not None for t in t4))
    check("★ type4 的 GuideParam 值命中 StreetActorInfoData.actorId",
          _npc_hits(t4))
    # type0 的证据
    check("★ type0 有 Parameter1 的是 0 条（无客户端上报路径）",
          sum(1 for t in t0 if S[t].get("Parameter1")) == 0)
    check("★ type0 有 TaskBgStory 的占全部（但那是显示文本）",
          sum(1 for t in t0 if S[t].get("TaskBgStory")) == len(t0))
    mainline_tail = [1102, 1103, 11031, 11040, 1104, 11041,
                     1106, 1107, 1108, 11080, 11091]
    dgs = {S[t].get("dgareaId") for t in mainline_tail if t in S}
    check("★ 主线尾段 11 条连续 type0 共用同一个 dgareaId（不可能各打一次副本）",
          len(dgs) == 1, dgs)

    print("\n[2] Type4：StreetInteract 命中 GuideParam NPC 才完成")
    st2 = clear_story(fresh("t04_a"))
    # ★ 快照必须在 start_task 之后取，否则客户端没见过 OP_ADD，
    #   后面的 OP_CHANGE 会被判成「改不存在的键」（测试假象）。
    story.start_story_task(st2, 1045, NOW)
    pre2 = pre_snapshot(uid_of("t04_a"))
    npc = story.street_task_npc(1045)
    check("1045 的 NPC 是 13164", npc == 13164, npc)
    # 错误的 actorId 不该完成
    res_bad = sev.apply_event(st2, sev.ev_street_interact(99999, 1, street_id=100))
    check("★ 交互别的 NPC 不会完成 1045", 1045 not in res_bad.hits,
          res_bad.hits)
    # 正确的 actorId 才完成
    res_ok = sev.apply_event(st2, sev.ev_street_interact(npc, 1, street_id=100))
    check("★ 交互 NPC %s 命中 1045" % npc, 1045 in res_ok.hits, res_ok.hits)
    frames = _finish_story_tasks(st2, list(res_ok.hits))
    check("★ 完成后 1045 归档", 1045 in st2.props["done_story_tasks"])
    check("★ PostTasks 1004 被启动", 1004 in st2.props["story_task_bag"])
    sc, errs = frames_safe(pre2, frames, "type4")
    check("Type4 完成路径的帧在客户端模型里不崩且无警告",
          not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[3] Type4：走真实 NPC RPC 全链路")
    st3 = clear_story(fresh("t04_b"))
    story.start_story_task(st3, 1045, NOW)
    pre3 = pre_snapshot(uid_of("t04_b"))
    idx = erpc.RPC_INDEX.get("Street_Actor_InteractID_Update_CS")
    frames3 = guarded(idx, msmod.mp_arr([13164, 1, 1, 1045, 1]), 1, uid_of("t04_b"))
    check("★ 走 Street_Actor_InteractID_Update_CS 后 1045 完成",
          1045 in st3.props["done_story_tasks"],
          story.story_state(st3.props, 1045))
    check("★ 1004 已启动（主线断点打通）",
          1004 in st3.props["story_task_bag"])
    sc3, errs3 = frames_safe(pre3, frames3, "type4_rpc")
    check("真实 RPC 链路的帧在客户端模型里不崩且无警告",
          not errs3 and not sc3.warnings, (errs3[:1], sc3.warnings[:1]))

    print("\n[4] Type4：13 条全部能判定（不漏），且 NPC 映射正确）")
    # ★ 期望 NPC 从**数据**直接算（StreetActorInfoData.actorId 命中 GuideParam），
    #   不经过被测函数 street_task_npc —— 否则映射错了也发现不了（循环断言）。
    expect = {}
    sa = gamedata.table("StreetActorInfoData") or {}
    actors = set()
    for v in sa.values():
        if isinstance(v, dict) and v.get("actorId") is not None:
            try:
                actors.add(int(v["actorId"]))
            except Exception:
                pass
    for t in t4:
        for g in (S[t].get("GuideParam") or []):
            if int(g) in actors:
                expect[t] = int(g)
                break
    check("能从数据算出 13 条 type4 的期望 NPC", len(expect) == 13, len(expect))
    wrong = {t: (story.street_task_npc(t), expect.get(t))
             for t in t4 if story.street_task_npc(t) != expect.get(t)}
    check("★ street_task_npc 的映射和数据一致", not wrong, wrong)
    miss = []
    for t in t4:
        stx = clear_story(fresh("t04_%d" % t))
        story.start_story_task(stx, t, NOW)
        res = sev.apply_event(stx, sev.ev_street_interact(expect[t], 1))
        if t not in res.hits:
            miss.append(t)
    check("★ 13 条 type4 全部能靠 StreetInteract 判定完成", not miss, miss)
    # 反向：交互**别的** NPC 不该完成任何一条
    false_hits = []
    for t in t4[:5]:
        sty = clear_story(fresh("t04n_%d" % t))
        story.start_story_task(sty, t, NOW)
        bad_npc = expect[t] + 999999
        res = sev.apply_event(sty, sev.ev_street_interact(bad_npc, 1))
        if t in res.hits:
            false_hits.append(t)
    check("★ 交互错误的 NPC 不会误完成", not false_hits, false_hits)

    print("\n[5] Type0：明确阻塞，不猜规则")
    # type0 不该被任何现有事件误完成
    st5 = clear_story(fresh("t04_c"))
    story.start_story_task(st5, 1102, NOW)
    for ev in (sev.ev_street_interact(1, 1), sev.ev_get_item(1, 1),
               sev.ev_dungeon_finish(1004, 1, 1)):
        sev.apply_event(st5, ev)
    check("★ type0 不会被误完成（没有实现规则）",
          story.story_state(st5.props, 1102) == sm.TASK_STATE_DOING,
          story.story_state(st5.props, 1102))

    print("\n[6] 覆盖率工具仍能跑（type4 应已计入可完成）")
    print("      （本次不改 tools/story_coverage.py 的 type0/4 口径，见 E6 记录）")

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


def _npc_hits(t4):
    sa = gamedata.table("StreetActorInfoData") or {}
    actors = set()
    for v in sa.values():
        if isinstance(v, dict) and v.get("actorId") is not None:
            try:
                actors.add(int(v["actorId"]))
            except Exception:
                pass
    return all((story.street_task_npc(t) or -1) in actors for t in t4)


def guarded(idx, params, rid, uid):
    from tests.frame_guard import guarded_handle
    return guarded_handle(idx, params, rid, uid=uid)


if __name__ == "__main__":
    sys.exit(run())
