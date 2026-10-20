#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""剧情触发引擎测试（TaskType 3 / 6 / 7）。

逆向结论（BDD + Lua 全查证）：
  StoryTask 1000 (TaskType=3, Parameter1=[1])
    -> TaskData[1] = {Description:"完成序章", TriggerEventList:[4001]}
    -> TaskEventData[4001] = {EventType:"FinishDungeon",
                             EventParam:{typeId:1004, modeId:1, level:1}}

  客户端 avatarstorytaskcomp.lua:667-681 只对 type 1/2/5 发完成请求，
  TaskType 3/7 必须由服务端监听游戏事件来推进。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_storyev_")
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


def run():
    print("\n[1] TaskType 分类常量")
    check("客户端驱动 = (1,2,5)", story.TASK_TYPE_CLIENT == (1, 2, 5))
    check("事件驱动 = (3,7)", story.TASK_TYPE_EVENT == (3, 7))
    check("链式 = (6,)", story.TASK_TYPE_CHAIN == (6,))

    print("\n[2] BDD 链条：StoryTask 1000 -> TaskData 1 -> TaskEvent 4001")
    st0 = gamedata.story_task(1000) or {}
    check("1000 TaskType = 3", st0.get("TaskType") == 3, st0.get("TaskType"))
    check("1000 Parameter1 = [1]", st0.get("Parameter1") == [1], st0.get("Parameter1"))
    td = gamedata.task_data(1) or {}
    check("TaskData[1] 描述 = 完成序章", td.get("Description") == "完成序章", td.get("Description"))
    check("TaskData[1] TriggerEventList = [4001]",
          td.get("TriggerEventList") == [4001], td.get("TriggerEventList"))
    ev = gamedata.task_event(4001) or {}
    check("事件 4001 类型 = FinishDungeon",
          ev.get("EventType") == "FinishDungeon", ev.get("EventType"))

    print("\n[3] event_matches 判定")
    check("完全匹配", story.event_matches(ev, "FinishDungeon",
                                      {"typeId": 1004, "modeId": 1, "level": 1}))
    check("类型不符不匹配", not story.event_matches(ev, "CharData",
                                                 {"typeId": 1004, "modeId": 1, "level": 1}))
    check("少一个键不匹配", not story.event_matches(ev, "FinishDungeon",
                                                 {"typeId": 1004, "modeId": 1}))
    check("值不对不匹配", not story.event_matches(ev, "FinishDungeon",
                                              {"typeId": 9999, "modeId": 1, "level": 1}))
    check("多余键不影响", story.event_matches(ev, "FinishDungeon",
                                          {"typeId": 1004, "modeId": 1, "level": 1,
                                           "extra": 7}))

    print("\n[4] 新号从 1000(Doing) 开始")
    st = fresh("se_a")
    check("1000 = Doing(3)", story.story_state(st.props, 1000) == story.TASK_STATE_DOING,
          story.story_state(st.props, 1000))
    check("1003 还没开始", story.story_state(st.props, 1003) == -1,
          story.story_state(st.props, 1003))

    print("\n[5] evaluate_event 只检测，不落状态")
    hits, _ = story.evaluate_event(st, "FinishDungeon",
                                   {"typeId": 1004, "modeId": 1, "level": 1})
    check("命中 1000", hits == [1000], hits)
    check("检测阶段不改状态",
          story.story_state(st.props, 1000) == story.TASK_STATE_DOING)

    print("\n[6] 走 RPC：Cmd_OfflineSpace_Finish_CS 完成闭环")
    st2 = fresh("se_b")
    frames = erpc.handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                         msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                         1, uid="se_b")
    check("1000 = Finished(6)",
          story.story_state(st2.props, 1000) == story.TASK_STATE_FINISHED,
          story.story_state(st2.props, 1000))
    check("1003 自动变 Doing(3)",
          story.story_state(st2.props, 1003) == story.TASK_STATE_DOING,
          story.story_state(st2.props, 1003))
    check("1000 进 done_story_tasks",
          1000 in (st2.props.get("done_story_tasks") or {}))
    check("1000 已从 story_task_bag 移除",
          1000 not in (st2.props.get("story_task_bag") or {}))
    check("有同步帧（不止 ACK）", len(frames) > 1, len(frames))

    print("\n[7] 参数不匹配时剧情不动")
    st3 = fresh("se_c")
    erpc.handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 9999, "modeId": 1, "level": 1}]),
                1, uid="se_c")
    check("1000 仍是 Doing",
          story.story_state(st3.props, 1000) == story.TASK_STATE_DOING,
          story.story_state(st3.props, 1000))
    check("1003 仍未开始", story.story_state(st3.props, 1003) == -1)

    print("\n[8] TaskType=7 也是 TaskData 驱动（7020 -> TaskData[16] -> CharData）")
    st4 = gamedata.story_task(7020) or {}
    check("7020 TaskType = 7", st4.get("TaskType") == 7, st4.get("TaskType"))
    td16 = gamedata.task_data(16) or {}
    check("TaskData[16] 有 TriggerEventList", bool(td16.get("TriggerEventList")),
          td16.get("TriggerEventList"))
    ev16 = None
    for eid in (td16.get("TriggerEventList") or []):
        ev16 = gamedata.task_event(eid)
        if ev16:
            break
    check("事件是 CharData", (ev16 or {}).get("EventType") == "CharData",
          (ev16 or {}).get("EventType"))
    check("事件参数带 level", "level" in ((ev16 or {}).get("EventParam") or {}))

    print("\n[9] check_chain_tasks 是纯检测")
    st5 = fresh("se_d")
    hits5, out5 = story.check_chain_tasks(st5)
    check("返回列表", isinstance(hits5, list), hits5)
    check("不返回同步项（纯检测）", out5 == [], out5)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
