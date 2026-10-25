#!/usr/bin/env python
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

from game_server import config, gamedata, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from tests.frame_guard import guarded_handle  # noqa: E402

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
    msmod.live_init_dict(uid)
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
    # ★ P1 阶段 N（审计 P0-4）：结算必须对应一次 StartGame（以前这里走的是伪造路径）
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                msmod.mp_arr([{"system_type": 1004, "storyID": 1}]), 1, uid="se_b")
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
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
    guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
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

    print("\n[10] 新增条目必须用 add（OP_ADD）")
    # 2026-10-20 实机踩过：给自定义 dict 加新键用 OP_CHANGE，客户端
    # __OpChange__ 里 oldVal 为 nil 直接报 "subproperty is nil" 并把变更丢掉。
    # 症状：剧情任务完成后 story_task_bag 删掉了，但 done_story_tasks 里没有，
    #       客户端 getTaskStateByTaskID 返回 -1（不是 6）。
    st6 = fresh("se_e")
    ch_start = story.start_story_task(st6, 1003, 1700000000)
    check("start_story_task 用 add",
          bool(ch_start) and ch_start[0][0] == "add", ch_start[:1])
    st7 = fresh("se_f")
    ch_fin = story.finish_story_task(st7, 1000, 1700000000)
    kinds = [c[0] for c in ch_fin]
    check("done_story_tasks 用 add", "add" in kinds, kinds)
    check("story_task_bag 用 del", "del" in kinds, kinds)
    # 新格子发奖也用 OP_ADD
    got = story.add_item(st7.props, 99999, 3)
    check("add_item 返回 (数量, 键, 是否新建)", isinstance(got, tuple) and len(got) == 3,
          got)
    if isinstance(got, tuple) and len(got) == 3:
        check("新道具 is_new = True", got[2] is True, got)

    print("\n[11] 街景 NPC 可见性前提（客户端 checkActorCondition）")
    # 客户端 system/streetsystem/streetactorbase.lua:109-129：
    #   if self.actorData.preTask then
    #     for taskId, taskState in pairs(preTask) do
    #       taskSatisfy = GlobalData.Avatar:getTaskStateByTaskID(taskId) == taskState
    #       if taskSatisfy then break end
    #     end
    #   end
    # 也就是「preTask 里任一 (taskId, state) 命中」才显示。
    # NPC 40004 的 preTask = {1003: 3}，所以要 1003 处于 Doing(3)。
    npc = gamedata.row("StreetActorInfoData", 40004) or {}
    pt = npc.get("preTask") or {}
    check("NPC 40004 preTask[1003] = 3", pt.get(1003) == 3 or pt.get("1003") == 3, pt)
    st8 = fresh("se_g")
    # ★ P1 阶段 N：先 StartGame（结算只信服务端副本上下文）
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                msmod.mp_arr([{"system_type": 1004, "storyID": 1}]), 1, uid="se_g")
    check("推进前 1003 不满足（-1）",
          story.story_state(st8.props, 1003) != 3,
          story.story_state(st8.props, 1003))
    guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                1, uid="se_g")
    check("推进后 1003 = Doing(3)，NPC 条件满足",
          story.story_state(st8.props, 1003) == 3,
          story.story_state(st8.props, 1003))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
