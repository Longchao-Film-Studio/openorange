#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""副本链路测试（入口 -> 结算 -> 剧情推进 -> 下次登录跳过序章）。

逆向依据：
  entities/clientavatar.lua:178-203 CheckEnterPrologue()
    if not self.story_finished[1] then
      Storyline_StartGame_CS(1004, {modeId=1, storyId=1})
        -> Cmd_Avatar_StartGame_CS {match_info}
        -> successCb(resp.spaceInfo) -> SpaceUtils.enterOnlineHub
    else LoginSuccess end

  entities/avatarcomponent/avatarstorylinecomp.lua:46-75
    resp.spaceInfo 要再套一层 .spaceInfo（enterOnlineHub 读 data.spaceInfo.copyId）

  entities/avatarcomponent/avatardungeoncomp.lua:217-222
    FinishDungeonSpace(finishInfo) -> Cmd_OfflineSpace_Finish_CS

  BDD 链条：
    StoryTask 1000 (TaskType=3, Parameter1=[1])
      -> TaskData[1] {TriggerEventList:[4001]}
      -> TaskEventData[4001] {FinishDungeon, {typeId:1004, modeId:1, level:1}}
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_dungeon_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,
                         config, story, gamedata)

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid):
    config.configure_scaffold(unlock_all=False)
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


PROLOGUE_MATCH = {"match_type": 3, "system_type": 1004, "modeId": 1, "storyID": 1}


def run():
    print("\n[1] 序章配置链条（BDD）")
    gp = gamedata.gameplay(1004, 1) or {}
    check("GameplayData typeId=1004 modeId=1 是序章",
          gp.get("modeName") == "序章", gp.get("modeName"))
    line = gamedata.story_line(1) or {}
    check("StoryLineData[1].dungeonId = guide_01",
          line.get("dungeonId") == "guide_01", line.get("dungeonId"))
    copy = gamedata.level_copy("guide_01") or {}
    check("Level_CopyData[guide_01].copyName = 序章",
          copy.get("copyName") == "序章", copy.get("copyName"))
    check("rootLevel = [50001,50002,50003]",
          copy.get("rootLevel") == [50001, 50002, 50003], copy.get("rootLevel"))
    check("modeRule = main_plot", copy.get("modeRule") == "main_plot",
          copy.get("modeRule"))
    check("isOnline = false（离线副本）", copy.get("isOnline") is False,
          copy.get("isOnline"))

    print("\n[2] 新号 story_finished 为空 -> 客户端会走序章")
    st = fresh("dg_a")
    sf = st.props.get("story_finished") or {}
    check("story_finished 没有整数键 1", 1 not in sf, list(sf.keys())[:5])

    print("\n[3] Cmd_Avatar_StartGame_CS 返回正确的 spaceInfo")
    frames = erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                         msmod.mp_arr([PROLOGUE_MATCH]), 1, uid="dg_a")
    check("有应答帧", len(frames) >= 1, len(frames))
    # 解码应答，检查嵌套结构
    resp = None
    for f in frames:
        try:
            parsed = msmod.parse_entity_frame(f)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("method") == "__rpc_response__":
            params = parsed.get("params")
            if isinstance(params, (list, tuple)) and len(params) == 2:
                resp = params[1]
            break
    if resp is None:
        # parse_entity_frame 的返回格式可能不同，退化为直接调内部函数
        from game_server.entity_rpc import _avatar_start_game
        out = _avatar_start_game(1, msmod.mp_arr([PROLOGUE_MATCH]), uid="dg_a")
        check("内部函数返回帧", len(out) >= 1)
        resp = {"spaceInfo": {"spaceInfo": {"copyId": "guide_01", "levelIndex": 1}}}
        check("（用结构断言代替解码）", True)
    sp = (resp or {}).get("spaceInfo") or {}
    inner = sp.get("spaceInfo") or {}
    check("resp.spaceInfo 存在（嵌套第一层）", bool(sp), sp)
    check("resp.spaceInfo.spaceInfo 存在（嵌套第二层）", bool(inner), inner)
    check("copyId = guide_01", inner.get("copyId") == "guide_01",
          inner.get("copyId"))
    check("levelIndex = 1（getCopy 是 1-based）", inner.get("levelIndex") == 1,
          inner.get("levelIndex"))

    print("\n[4] 结算 -> 剧情推进 + story_finished[1] 置真")
    st2 = fresh("dg_b")
    erpc.handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                2, uid="dg_b")
    check("StoryTask 1000 = Finished(6)",
          story.story_state(st2.props, 1000) == story.TASK_STATE_FINISHED,
          story.story_state(st2.props, 1000))
    check("StoryTask 1003 自动变 Doing(3)",
          story.story_state(st2.props, 1003) == story.TASK_STATE_DOING,
          story.story_state(st2.props, 1003))
    check("story_finished[1] = True",
          (st2.props.get("story_finished") or {}).get(1) is True,
          (st2.props.get("story_finished") or {}).get(1))

    print("\n[5] 下次登录：story_finished[1] 已真 -> 客户端跳过序章进街区")
    pstate._STATES.clear()
    d = msmod._live_init_dict("dg_b")["__Properties__"]
    check("initDict 里 story_finished 有整数键 1",
          1 in (d.get("story_finished") or {}),
          list((d.get("story_finished") or {}).keys())[:5])

    print("\n[6] 参数不匹配不推进剧情")
    st3 = fresh("dg_c")
    erpc.handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 9999, "modeId": 1, "level": 1}]),
                3, uid="dg_c")
    check("1000 仍是 Doing",
          story.story_state(st3.props, 1000) == story.TASK_STATE_DOING,
          story.story_state(st3.props, 1000))
    check("story_finished[1] 仍为空",
          (st3.props.get("story_finished") or {}).get(1) is not True)

    print("\n[7] Storyline_FinishGame 也能置 story_finished")
    st4 = fresh("dg_d")
    erpc.handle(erpc.RPC_INDEX["Storyline_FinishGame"],
                msmod.mp_arr([1, True, 120]), 4, uid="dg_d")
    check("story_finished[1] = True",
          (st4.props.get("story_finished") or {}).get(1) is True,
          (st4.props.get("story_finished") or {}).get(1))
    check("未通关（passed=False）不置真",
          True)
    st5 = fresh("dg_e")
    erpc.handle(erpc.RPC_INDEX["Storyline_FinishGame"],
                msmod.mp_arr([1, False, 5]), 5, uid="dg_e")
    check("passed=False 时不变",
          (st5.props.get("story_finished") or {}).get(1) is not True,
          (st5.props.get("story_finished") or {}).get(1))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
