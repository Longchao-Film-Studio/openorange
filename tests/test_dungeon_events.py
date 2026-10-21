#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""副本结算 -> 剧情事件 的完整参数覆盖测试。

背景（2026-10-21）：
  FinishDungeon 有 77 个事件，story.event_matches 要求事件配置里的**每个**
  参数都出现在服务端给的 params 里，否则不匹配。之前我们只给
  {typeId, modeId, level}，77 个里只匹配得上 11 个。

  补齐后给的字段：
    typeId        全部
    areaid        57 个事件需要 —— 从当前关卡反查（EnterDungeonData.StageId->DungeonId）
    levelId        4 个事件需要 —— 当前关卡 id
    instructionid  6 个事件需要 —— 本次副本激活过的战斗教学
    first_pass     6 个事件需要 —— 首次通关
    isWin          1 个事件需要
    modeId/level   原有

  验证用真实数据：副本 dgarea_arcade_04 的关卡 52108 -> areaid 10304，
  对应事件 5009 = FinishDungeon{typeId:1000, areaid:10304}，
  该事件属于剧情任务 5031（TaskType 3, Parameter1=[500014]）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_dgev_")
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


COPY = "dgarea_arcade_04"
LEVEL = 52108
AREA = 10304
TASK = 5031
EVENT = 5009


def run():
    print("\n[0] BDD 前提校验")
    check("副本 %s 存在" % COPY, bool(gamedata.level_copy(COPY)))
    check("关卡 %d -> areaid %d" % (LEVEL, AREA),
          gamedata.area_of_level(LEVEL) == AREA,
          gamedata.area_of_level(LEVEL))
    ev = gamedata.task_event(EVENT) or {}
    check("事件 %d 是 FinishDungeon{typeId:1000, areaid:%d}" % (EVENT, AREA),
          ev.get("EventType") == "FinishDungeon"
          and (ev.get("EventParam") or {}).get("areaid") == AREA, ev)
    st_cfg = gamedata.story_task(TASK) or {}
    check("任务 %d 是 TaskType 3" % TASK, st_cfg.get("TaskType") == 3,
          st_cfg.get("TaskType"))

    print("\n[1] 进副本时记下关卡和区域")
    st = fresh("dgev_a")
    story.start_story_task(st, TASK, 0)
    st.save()
    erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                msmod.mp_arr([{"copyId": COPY, "system_type": 1000, "modeId": 1}]),
                1, uid="dgev_a")
    check("_oo_cur_copy = %s" % COPY, st.props.get("_oo_cur_copy") == COPY)
    check("_oo_cur_level = %d" % LEVEL,
          st.props.get("_oo_cur_level") == LEVEL, st.props.get("_oo_cur_level"))
    check("_oo_cur_area = %d" % AREA,
          st.props.get("_oo_cur_area") == AREA, st.props.get("_oo_cur_area"))

    print("\n[2] 战斗中激活教学 -> 记下 instructionid")
    erpc.handle(erpc.RPC_INDEX["BattleTeach_Activate_CS"],
                msmod.mp_arr([101]), 2, uid="dgev_a")
    check("_oo_cur_teach = 101", st.props.get("_oo_cur_teach") == 101,
          st.props.get("_oo_cur_teach"))

    print("\n[3] 结算 -> 事件参数带 areaid，任务 5031 完成")
    info = {"toStreetId": 101, "toStreetPosId": 0, "finishReason": 5,
            "userList": {"dgev_a": 1},
            "dgev_a": {"win": True, "battle_items": {}}}
    erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                msmod.mp_arr([info]), 3, uid="dgev_a")
    check("任务 %d = Finished" % TASK,
          story.story_state(st.props, TASK) == story.TASK_STATE_FINISHED,
          story.story_state(st.props, TASK))

    print("\n[4] 首通记录 + 上下文清理")
    check("_oo_cleared_levels 记下了 %d" % LEVEL,
          isinstance(st.props.get("_oo_cleared_levels"), dict)
          and str(LEVEL) in st.props["_oo_cleared_levels"],
          st.props.get("_oo_cleared_levels"))
    for k in ("_oo_cur_copy", "_oo_cur_match", "_oo_cur_level",
              "_oo_cur_area", "_oo_cur_teach"):
        check("%s 已清" % k, k not in st.props)

    print("\n[5] 第二次通关不再是 first_pass")
    st2 = fresh("dgev_b")
    st2.props["_oo_cleared_levels"] = {str(LEVEL): 1}
    st2.save()
    ev_params = {}
    orig = story.evaluate_event

    def spy(s, etype, params, now=None):
        ev_params.update(params)
        return orig(s, etype, params, now)

    story.evaluate_event = spy
    try:
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                    msmod.mp_arr([{"copyId": COPY, "system_type": 1000,
                                   "modeId": 1}]), 1, uid="dgev_b")
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                    msmod.mp_arr([dict(info, userList={"dgev_b": 1},
                                       dgev_b={"win": True, "battle_items": {}})]),
                    2, uid="dgev_b")
    finally:
        story.evaluate_event = orig
    check("首次已通关 -> 不带 first_pass", "first_pass" not in ev_params,
          ev_params)
    check("仍带 areaid", ev_params.get("areaid") == AREA, ev_params)
    check("仍带 levelId", ev_params.get("levelId") == LEVEL, ev_params)
    check("仍带 isWin=1", ev_params.get("isWin") == 1, ev_params)

    print("\n[6] 首通时带 first_pass")
    st3 = fresh("dgev_c")
    ev3 = {}
    orig2 = story.evaluate_event

    def spy2(s, etype, params, now=None):
        ev3.update(params)
        return orig2(s, etype, params, now)

    story.evaluate_event = spy2
    try:
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                    msmod.mp_arr([{"copyId": COPY, "system_type": 1000,
                                   "modeId": 1}]), 1, uid="dgev_c")
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                    msmod.mp_arr([dict(info, userList={"dgev_c": 1},
                                       dgev_c={"win": True, "battle_items": {}})]),
                    2, uid="dgev_c")
    finally:
        story.evaluate_event = orig2
    check("首次通关 -> first_pass = 1", ev3.get("first_pass") == 1, ev3)

    print("\n[7] 失败时 isWin = 0")
    st4 = fresh("dgev_d")
    ev4 = {}
    orig3 = story.evaluate_event

    def spy3(s, etype, params, now=None):
        ev4.update(params)
        return orig3(s, etype, params, now)

    story.evaluate_event = spy3
    try:
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                    msmod.mp_arr([{"copyId": COPY, "system_type": 1000,
                                   "modeId": 1}]), 1, uid="dgev_d")
        erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                    msmod.mp_arr([dict(info, userList={"dgev_d": 1},
                                       dgev_d={"win": False, "battle_items": {}})]),
                    2, uid="dgev_d")
    finally:
        story.evaluate_event = orig3
    check("失败 -> isWin = 0", ev4.get("isWin") == 0, ev4)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
