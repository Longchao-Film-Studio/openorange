#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 2：位置持久化 + 街景交互/红点/追踪。

逆向依据：
  system/streetsystem/manager/streetsystemmgrbase.lua:561-566
    local posinfo = obj:getStreetPosInfo()   -- Avatar.street_pos_info
    if posinfo and posinfo.x then remotePosInfo = copy(posinfo) end
  :654 用 remotePosInfo.x/y/z 定位、remotePosInfo.dir 定朝向

  entities/avatarcomponent/avatarstreetactorcomp.lua:19-40
    Street_Actor_InteractID_Update_CS {actor_id, interact_group, interact_id, taskId?, interact_id}
  交互状态存 StreetInfo.inter_actor_info（IntDictKeyInt）

  entities/avatarcomponent/avatarstreetcomp.lua:581-587
    Street_Redpoint_Update_CS {key, value} -> StreetInfoDict.street_redpoint
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_pos_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, entity_rpc as erpc, player_state as pstate, config, story

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] street_pos_info 必须是 DoubleDict（不是 Pos）")
    # 2026-10-20 实机验证：
    #   用 Pos（结构体）-> 客户端必崩：
    #     class ClientAvatar function init must have a boolean return value
    #     traceback -> AvatarBagComp:refreshLocalBagData
    #   用 DoubleDict（Dict<string,double>）-> 正常，且位置真的恢复。
    #   依据：street_pos_info 在 PropertyCallbacks 里声明的是 customDict；
    #   core/propertysync/customdict.lua:21-30 的 __index 直接读 _properties[k]，
    #   所以客户端 streetsystemmgrbase.lua:562 的 posinfo.x 能正常工作。
    config.configure_scaffold()
    d = msmod._live_init_dict("pos_a")["__Properties__"]
    spi = d.get("street_pos_info") or {}
    check("street_pos_info 存在", bool(spi))
    check("__tp__ = DoubleDict", spi.get("__tp__") == "DoubleDict",
          spi.get("__tp__"))

    print("\n[1b] 回归：initDict 里不能有服务端私有字段")
    # 2026-10-20 踩过：_oo_street_pos 是普通 dict，被写进 initDict，
    # 客户端 initProperty 对每个表值都 assert(__tp__ ~= nil)，
    # 于是 ClientAvatar:init 抛错返回非布尔，表现是
    #   自动进入游戏 -> 被踢回开始页 -> 再自动进 -> 再被踢 的死循环。
    bad = [k for k in d if isinstance(k, str) and k.startswith("_oo_")]
    check("initDict 里没有 _oo_ 私有字段", not bad, bad)
    no_tp = [k for k, v in d.items() if isinstance(v, dict) and "__tp__" not in v]
    check("每个表值都带 __tp__", not no_tp, no_tp[:5])

    print("\n[2] AOI 上报 -> 落盘 -> 重登恢复")
    st = pstate.get_state(uid="pos_a")
    erpc.handle(1920167376, msmod.mp_arr([12.5, -3.25, 88.75, 1.57]), 0,
                uid="pos_a")
    erpc._LAST_SAVE.clear()
    erpc.handle(294469585,
                msmod.mp_arr([101, {"x": 12.5, "y": -3.25, "z": 88.75, "dir": 1.57}]),
                0, uid="pos_a")
    check("_oo_street_pos 已记录",
          (st.props.get("_oo_street_pos") or {}).get("x") == 12.5,
          st.props.get("_oo_street_pos"))
    pstate._STATES.clear()
    d2 = msmod._live_init_dict("pos_a")["__Properties__"]
    spi2 = d2.get("street_pos_info") or {}
    check("重登后 x 恢复", abs(spi2.get("x", 0) - 12.5) < 1e-6, spi2)
    check("重登后 z 恢复", abs(spi2.get("z", 0) - 88.75) < 1e-6, spi2)
    check("重登后 dir 也带上", abs(spi2.get("dir", 0) - 1.57) < 1e-6, spi2)

    print("\n[3] 节流：短时间内第二次上报不重复写盘")
    erpc._LAST_SAVE.clear()
    st = pstate.get_state(uid="pos_a")
    erpc.handle(1920167376, msmod.mp_arr([1, 2, 3, 0]), 0, uid="pos_a")
    first = erpc._LAST_SAVE.get("pos_a")
    erpc.handle(1920167376, msmod.mp_arr([4, 5, 6, 0]), 0, uid="pos_a")
    check("5 秒内不重复落盘", erpc._LAST_SAVE.get("pos_a") == first)
    check("内存里的值仍然更新",
          (st.props.get("_oo_street_pos") or {}).get("x") == 4.0)

    print("\n[4] 街景 NPC 交互记录到 inter_actor_info")
    st.props["street_id"] = 101
    frames = erpc.handle(erpc.RPC_INDEX["Street_Actor_InteractID_Update_CS"],
                         msmod.mp_arr([40004, 40004, 400041, None, 400041]), 1,
                         uid="pos_a")
    si = st.props.get("street_info") or {}
    entry = si.get(101) or {}
    ia = entry.get("inter_actor_info") or {}
    check("inter_actor_info[40004] = 400041", ia.get(40004) == 400041, ia)
    check("返回同步帧", len(frames) >= 2, len(frames))

    print("\n[5] 交互同时完成命中的剧情任务")
    story.start_story_task(st, 1003, 1700000000)
    # 1003 的 Parameter1 是 [400041]，TaskType=2
    erpc.handle(erpc.RPC_INDEX["Street_Actor_InteractID_Update_CS"],
                msmod.mp_arr([40004, 40004, 400041, None, 400041]), 2,
                uid="pos_a")
    check("任务 1003 已完成",
          story.story_state(st.props, 1003) == story.TASK_STATE_FINISHED,
          story.story_state(st.props, 1003))

    print("\n[6] 红点")
    frames = erpc.handle(erpc.RPC_INDEX["Street_Redpoint_Update_CS"],
                         msmod.mp_arr([7, 1]), 3, uid="pos_a")
    rp = (st.props.get("street_info") or {}).get("street_redpoint") or {}
    check("street_redpoint[7] = 1", rp.get(7) == 1, rp)
    check("返回同步帧", len(frames) >= 2, len(frames))

    print("\n[7] 追踪任务")
    frames = erpc.handle(erpc.RPC_INDEX["updateStreetTraceTask_CS"],
                         msmod.mp_arr([1003]), 4, uid="pos_a")
    check("street_trace_taskid = 1003",
          st.props.get("street_trace_taskid") == 1003,
          st.props.get("street_trace_taskid"))

    print("\n[7b] 阴阳界切换")
    # 逆向：avatarstreetcomp.lua:560-562 getCurStreetYinYangEnterTime 读
    #   StreetInfoDict.surface_enter_time；streetsystemmgrbase.lua:679-685 用它定 worldIndex。
    frames = erpc.handle(erpc.RPC_INDEX["RequestEnterStreetSurface_CS"],
                         msmod.mp_arr([1]), 5, uid="pos_a")
    si2 = st.props.get("street_info") or {}
    t_enter = si2.get("surface_enter_time") or 0
    check("进阴界 surface_enter_time > 0", t_enter > 0, t_enter)
    check("返回同步帧", len(frames) >= 2, len(frames))
    erpc.handle(2137482665, msmod.mp_arr([]), 0, uid="pos_a")
    check("离开阴界归 0",
          (st.props.get("street_info") or {}).get("surface_enter_time") == 0)

    print("\n[7c] 街景宝箱")
    # 逆向：StreetInfo.treasure_box_info（IntDictKeyInt），0 < state 即已开
    frames = erpc.handle(erpc.RPC_INDEX["RequestStreetTreasure_CS"],
                         msmod.mp_arr([10157]), 6, uid="pos_a")
    entry2 = (st.props.get("street_info") or {}).get(101) or {}
    tb = entry2.get("treasure_box_info") or {}
    check("treasure_box_info[10157] = 1", tb.get(10157) == 1, tb)
    check("返回同步帧", len(frames) >= 2, len(frames))
    erpc.handle(1282426820, msmod.mp_arr([10158]), 0, uid="pos_a")
    entry3 = (st.props.get("street_info") or {}).get(101) or {}
    tb3 = entry3.get("treasure_box_info") or {}
    check("TouchTreasure 也记录", tb3.get(10158) == 1, tb3)

    print("\n[8] 清理宝箱 actor（单向）")
    erpc.handle(1675033651, msmod.mp_arr([101]), 0, uid="pos_a")
    check("不崩", True)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())