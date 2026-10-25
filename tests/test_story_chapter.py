#!/usr/bin/env python
"""P1 阶段 G：章节 / 收集 / 奖励（AvatarStorylineComp 的三个属性）。

★ 客户端契约（generated/config.lua:9958-9983）
  changed.story_finished            -> on_story_finished_changed（空函数）
  changed.story_max_collect_num     -> on_story_max_collect_num_changed（发事件）
  changed.story_reward_list         -> on_story_reward_list_changed（空函数）
  entryAdded.story_finished         -> on_story_finished_added（真正干活的那个）

★ 读取方
  avatarstorylinecomp.lua:105-107  getLevelIsCompleteByID(storyLineID)  读 story_finished
  avatarstorylinecomp.lua:97-99    getMaxCollectNumByLevel(storyLineID) 读 story_max_collect_num
  avatarstorylinecomp.lua:101-103   getLevelRewardedList(storyLineID)   读 story_reward_list
  clientavatar.lua:180 / streetstage.lua:169 / loginmanager.lua:130  读 story_finished[1]

★ 数据
  StoryLineData 17 条，键是 id（1..20），字段有 dungeonId / collectItem /
  collectAward1 / passAward / chapterId / preLevel。

★ 不变量
  1. story_finished[storyLineId] 由 copyId -> StoryLineData.dungeonId 反查决定，
     不再写死 [1]。
  2. 首次用 OP_ADD（entryAdded 才会调 on_story_finished_added），复通用 OP_CHANGE。
  3. story_max_collect_num / story_reward_list 只增不改、幂等。

用法: python tests/test_story_chapter.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_ch_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import (
    gamedata,  # noqa: E402
    story,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
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
    return f"{name}_{_TAG}"


def fresh(name):
    u = uid_of(name)
    msmod.live_init_dict(u)
    return pstate.get_state(uid=u)


def clear_story(st):
    for prop in ("story_task_bag", "done_story_tasks",
                 "story_max_collect_num", "story_reward_list"):
        d = st.props.get(prop)
        if isinstance(d, dict):
            for k in [k for k in d if k not in ("__id__", "__tp__")]:
                d.pop(k, None)
    return st


def frame_op(frame):
    """解一条属性同步帧的 (propertyId, optype, name)。"""
    fl = msmod.pb_parse(bytes(frame[6:]))
    nm = fl.get(4, [b""])[0]
    if isinstance(nm, (bytes, bytearray)):
        nm = nm.decode("utf-8", "replace")
    return fl.get(2, [None])[0], fl.get(3, [None])[0], nm


def sync_frames(st, descs):
    from game_server.story.rpc import _story_sync_frames
    return _story_sync_frames(st, descs)


def run():
    global _TAG
    _TAG = f"{random.randint(0, 0xFFFF):04x}"
    del passed[:]
    del failed[:]

    SL = gamedata.table("StoryLineData") or {}
    SL = list(SL.values()) if isinstance(SL, dict) else SL
    print(f'\n[1] 数据事实：StoryLineData 共 {len(SL)} 条')
    check("StoryLineData 共 17 条", len(SL) == 17, len(SL))
    check("★ 17 条全有 passAward", all(r.get("passAward") for r in SL))
    check("★ 15 条有 collectItem", sum(1 for r in SL if r.get("collectItem")) == 15)
    check("guide_01 对应 id=1",
          (story.story_line_of_copy("guide_01") or {}).get("id") == 1)
    check("chapter_00_02 对应 id=6",
          (story.story_line_of_copy("chapter_00_02") or {}).get("id") == 6)

    print("\n[2] story_finished：按 copyId 反查，不再写死 [1]")
    st2 = fresh("ch_a")
    story.start_story_task(st2, 1000, NOW)
    pre2 = pre_snapshot(uid_of("ch_a"))
    descs = story.mark_story_line_finished(st2, "guide_01")
    check("★ guide_01 -> story_finished[1]=True",
          (st2.props.get("story_finished") or {}).get(1) is True,
          (st2.props.get("story_finished") or {}).get(1))
    _pid, _op, _nm = frame_op(descs[0]) if descs else (None, None, None)
    check("★ 首次用 OP_ADD（entryAdded 才会调 on_story_finished_added）",
          _op == msmod.OP_ADD and _nm == "1", (_pid, _op, _nm))
    frames = sync_frames(st2, descs)
    sc, errs = frames_safe(pre2, frames, "story_finished")
    check("story_finished 帧在客户端模型里不崩且无警告",
          not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))
    # 复通：OP_CHANGE
    descs2 = story.mark_story_line_finished(st2, "guide_01")
    check("★ 复通返回空（幂等）", descs2 == [], descs2)
    # 另一条线：chapter_00_02 -> id=6
    descs3 = story.mark_story_line_finished(st2, "chapter_00_02")
    check("★ chapter_00_02 -> story_finished[6]=True",
          (st2.props.get("story_finished") or {}).get(6) is True,
          (st2.props.get("story_finished") or {}).get(6))
    _p3, _o3, _n3 = frame_op(descs3[0]) if descs3 else (None, None, None)
    check("★ 新线首次也是 OP_ADD",
          _o3 == msmod.OP_ADD and _n3 == "6", (_p3, _o3, _n3))
    check("★ 未知 copyId 不产生帧",
          story.mark_story_line_finished(st2, "no_such_copy") == [])

    print("\n[3] 走真实 RPC：副本结算后 story_finished 置真")
    st3 = clear_story(fresh("ch_b"))
    pre3 = pre_snapshot(uid_of("ch_b"))
    story.start_story_task(st3, 1000, NOW)
    # ★ P1 阶段 N（审计 P0-4）：结算必须对应一次 StartGame
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 1004, "storyID": 1}]), 1,
                   uid=uid_of("ch_b"))
    pre3 = pre_snapshot(uid_of("ch_b"))
    frames3 = guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                          msmod.mp_arr([{"typeId": 1004, "modeId": 1,
                                         "level": 1}]), 1, uid=uid_of("ch_b"))
    check("★ 结算后 story_finished[1]=True",
          (st3.props.get("story_finished") or {}).get(1) is True,
          (st3.props.get("story_finished") or {}).get(1))
    sc3, errs3 = frames_safe(pre3, frames3, "settle")
    check("★ 结算帧在客户端模型里不崩且无警告",
          not errs3 and not sc3.warnings, (errs3[:1], sc3.warnings[:1]))

    print("\n[4] story_max_collect_num：按 collectItem 补全")
    st4 = clear_story(fresh("ch_c"))
    pre4 = pre_snapshot(uid_of("ch_c"))
    descs4 = story.sync_story_max_collect_num(st4)
    check("★ 产生了收集数帧", bool(descs4), len(descs4))
    mc = st4.props.get("story_max_collect_num") or {}
    filled = {k: v for k, v in mc.items() if k not in ("__id__", "__tp__")}
    check("★ story_max_collect_num 不再恒空", bool(filled), filled)
    # 期望值：有 collectItem 的线
    want = {int(r["id"]) for r in SL if r.get("collectItem")}
    check("★ 覆盖的线和有 collectItem 的一致",
          set(filled.keys()) == want,
          (sorted(set(filled.keys()))[:8], sorted(want)[:8]))
    check("★ 值都是正数", all(v > 0 for v in filled.values()), filled)
    # 幂等
    descs4b = story.sync_story_max_collect_num(st4)
    check("★ 第二次同步返回空（幂等）", descs4b == [], len(descs4b))
    frames4 = sync_frames(st4, descs4)
    sc4, errs4 = frames_safe(pre4, frames4, "max_collect")
    check("收集数帧在客户端模型里不崩且无警告",
          not errs4 and not sc4.warnings, (errs4[:1], sc4.warnings[:1]))

    print("\n[5] story_reward_list：按 StoryLineData 补全")
    st5 = clear_story(fresh("ch_d"))
    pre5 = pre_snapshot(uid_of("ch_d"))
    descs5 = story.sync_story_reward_list(st5)
    check("★ 产生了奖励列表帧", bool(descs5), len(descs5))
    rl = st5.props.get("story_reward_list") or {}
    filled5 = {k: v for k, v in rl.items() if k not in ("__id__", "__tp__")}
    check("★ story_reward_list 不再恒空", bool(filled5), list(filled5)[:8])
    check("★ 每条线的值是带 __id__ 的空列表",
          all(isinstance(v, dict) and "__id__" in v for v in filled5.values()),
          list(filled5.values())[:2])
    descs5b = story.sync_story_reward_list(st5)
    check("★ 第二次同步返回空（幂等）", descs5b == [], len(descs5b))
    frames5 = sync_frames(st5, descs5)
    sc5, errs5 = frames_safe(pre5, frames5, "reward_list")
    check("奖励列表帧在客户端模型里不崩且无警告",
          not errs5 and not sc5.warnings, (errs5[:1], sc5.warnings[:1]))

    print("\n[6] 登录时自动补全（live_init_dict 里跑）")
    st6 = fresh("ch_e")
    _mc6 = [k for k in (st6.props.get("story_max_collect_num") or {})
            if k not in ("__id__", "__tp__")]
    check("★ 登录后 story_max_collect_num 有值", bool(_mc6), _mc6[:6])
    _rl6 = [k for k in (st6.props.get("story_reward_list") or {})
            if k not in ("__id__", "__tp__")]
    check("★ 登录后 story_reward_list 有值", bool(_rl6), _rl6[:6])

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
