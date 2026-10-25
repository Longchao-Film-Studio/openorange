#!/usr/bin/env python
"""P1 阶段 E / 阶段 P：TaskType 0 / TaskType 4。

★ Type4（13 条）—— 2026-10-22 阶段 P 已纠正完成条件
  正确：**切街到 `Parameter1[0]` 指定的街区即完成**。
  依据（docs/storytaskresearch.md §16 / §19）：
    - 13 条 type4 的 Parameter1 全是目标街区 id
    - 能验证的 4 条 transferStreet 与 Parameter1 4/4 一致：
        1045->100、4006->101、4063->101、6064->102
    - 客户端**不上报** type4（getTaskIdByParamInt 只筛 type2）
    - 原实现「和 GuideParam[0] 交互才完成」是错的：1045 的 GuideParam[0]=13164
      在 StreetActorInteractData 里没有条目，客户端永远发不出这个交互。

★ Type0（270 条）—— 由 SpaceTask 驱动，见 tests/test_story_spacetask.py
  本文件只断言「不会被其它事件误完成」。

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
from game_server import (
    gamedata,  # noqa: E402
    story,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
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
    return f"{name}_{_TAG}"


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


def guarded(idx, params, rid, uid):
    from tests.frame_guard import guarded_handle
    return guarded_handle(idx, params, rid, uid=uid)


def _switch(st_name, street_id, rid):
    """走真实切街 RPC。"""
    idx = erpc.RPC_INDEX.get("RequestSwitchStreet_CS")
    return guarded(idx, msmod.mp_arr([street_id]), rid, uid_of(st_name))


def run():
    global _TAG
    _TAG = f"{random.randint(0, 0xFFFF):04x}"
    del passed[:]
    del failed[:]

    S = gamedata.table("StoryTaskData") or {}
    S = {int(v["TaskID"]): v for v in S.values()
         if isinstance(v, dict) and v.get("TaskID") is not None}
    t4 = sorted(t for t, v in S.items() if v.get("TaskType") == 4)
    t0 = sorted(t for t, v in S.items() if v.get("TaskType") == 0)

    print(f'\n[1] 数据事实：type4 共 {len(t4)} 条，type0 共 {len(t0)} 条')
    check("type4 共 13 条", len(t4) == 13, len(t4))
    check("type0 共 270 条", len(t0) == 270, len(t0))
    check("★ 13 条 type4 全部有 Parameter1（目标街区）",
          all(S[t].get("Parameter1") for t in t4))
    check("★ 13 条 type4 的 Parameter1 全是合法 StreetData 键",
          _p1_all_streets(t4, S))
    check("★ 13 条 type4 的 Parameter1 互不重复或可共存",
          len({int(S[t]["Parameter1"][0]) for t in t4}) > 0)
    check("★ type0 有 Parameter1 的是 0 条（无客户端上报路径）",
          sum(1 for t in t0 if S[t].get("Parameter1")) == 0)
    mainline_tail = [1102, 1103, 11031, 11040, 1104, 11041,
                     1106, 1107, 1108, 11080, 11091]
    dgs = {S[t].get("dgareaId") for t in mainline_tail if t in S}
    check("★ 主线尾段 11 条连续 type0 共用同一个 dgareaId",
          len(dgs) == 1, dgs)

    print("\n[2] Type4：切街到 Parameter1 才完成（核心）")
    st2 = clear_story(fresh("t04_a"))
    story.start_story_task(st2, 1045, NOW)
    p1 = int(S[1045]["Parameter1"][0])
    check("1045 的 Parameter1 是 100（龙潮阁）", p1 == 100, p1)
    # 先到一个无关街区，不应完成
    st2.props["street_id"] = 101
    hits_wrong = story.street_arrival_tasks(st2, 99)
    check("★ 切到无关街区 99 不会命中 1045", 1045 not in hits_wrong, hits_wrong)
    hits_ok = story.street_arrival_tasks(st2, p1)
    check("★ 切到 Parameter1 才命中 1045", 1045 in hits_ok, hits_ok)
    pre2 = pre_snapshot(uid_of("t04_a"))
    frames = _finish_story_tasks(st2, hits_ok, source="street_arrival")
    check("★ 完成后 1045 归档", 1045 in st2.props["done_story_tasks"])
    check("★ PostTasks 1004 被启动（主线断点打通）",
          1004 in st2.props["story_task_bag"])
    sc, errs = frames_safe(pre2, frames, "type4")
    check("Type4 完成路径的帧在客户端模型里不崩且无警告",
          not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[3] Type4：走真实 RequestSwitchStreet_CS 全链路")
    st3 = clear_story(fresh("t04_b"))
    story.start_story_task(st3, 1045, NOW)
    guarded(erpc.RPC_INDEX.get("RpcEnterStreet"), msmod.mp_arr([101]), 1, uid_of("t04_b"))
    check("进街区 101", st3.props.get("street_id") == 101, st3.props.get("street_id"))
    check("切街前 1045 是 Doing",
          story.story_state(st3.props, 1045) == sm.TASK_STATE_DOING,
          story.story_state(st3.props, 1045))
    pre3 = pre_snapshot(uid_of("t04_b"))
    frames3 = _switch("t04_b", 100, 2)
    check("★ 走 RequestSwitchStreet_CS 切到 100 后 1045 完成",
          1045 in st3.props["done_story_tasks"],
          story.story_state(st3.props, 1045))
    check("★ 1004 已启动（实机断链已修）",
          1004 in st3.props["story_task_bag"])
    sc3, errs3 = frames_safe(pre3, frames3, "type4_rpc")
    check("真实 RPC 链路的帧在客户端模型里不崩且无警告",
          not errs3 and not sc3.warnings, (errs3[:1], sc3.warnings[:1]))

    print("\n[4] Type4：13 条全部能靠「切街到达」判定（不漏、不误）")
    miss = []
    for t in t4:
        stx = clear_story(fresh(f't04_{t}'))
        story.start_story_task(stx, t, NOW)
        want = int(S[t]["Parameter1"][0])
        hits = story.street_arrival_tasks(stx, want)
        if t not in hits:
            miss.append(t)
    check("★ 13 条 type4 全部能靠切街到 Parameter1 判定完成", not miss, miss)
    # 反向：切到无关街区不该命中任何一条
    false_hits = []
    for t in t4:
        sty = clear_story(fresh(f't04n_{t}'))
        story.start_story_task(sty, t, NOW)
        want = int(S[t]["Parameter1"][0])
        bad_street = want + 888888
        if t in story.street_arrival_tasks(sty, bad_street):
            false_hits.append(t)
    check("★ 切到错误街区不会误完成", not false_hits, false_hits)
    # 不在包里的 type4 不该被判定
    stz = clear_story(fresh("t04_z"))
    check("★ 不在包里的 type4 不会被判定完成",
          story.street_arrival_tasks(stz, 100) == [],
          story.street_arrival_tasks(stz, 100))

    print("\n[5] Type4：StreetInteract 不再完成 type4（旧错误路径已断）")
    st5b = clear_story(fresh("t04_c2"))
    story.start_story_task(st5b, 1045, NOW)
    res_old = sev.apply_event(st5b, sev.ev_street_interact(13164, 1, street_id=100))
    check("★ 交互 13164 不再命中 1045（旧实现已删除）",
          1045 not in res_old.hits, res_old.hits)

    print("\n[6] Type0：不会被其它事件误完成")
    st6 = clear_story(fresh("t04_d"))
    story.start_story_task(st6, 1102, NOW)
    for ev in (sev.ev_street_interact(1, 1), sev.ev_get_item(1, 1),
               sev.ev_dungeon_finish(1004, 1, 1)):
        sev.apply_event(st6, ev)
    check("★ type0 不会被街区/道具/副本事件误完成",
          story.story_state(st6.props, 1102) == sm.TASK_STATE_DOING,
          story.story_state(st6.props, 1102))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


def _p1_all_streets(t4, S):
    sd = gamedata.table("StreetData") or {}
    keys = {str(k) for k in sd.keys()}
    for t in t4:
        p1 = S[t].get("Parameter1") or []
        if not p1 or str(p1[0]) not in keys:
            return False
    return True


if __name__ == "__main__":
    sys.exit(run())