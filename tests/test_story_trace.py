#!/usr/bin/env python
"""P1 阶段 N：追踪目标的两个 bug（实机反馈）+ 防伪造回归。

★ 这三个 bug 都是 2026-10-22 实机反馈暴露的

  Bug C：客户端「取消追踪」发的是 SystemConst.StreetIllegalStreetID = -1000
         （systemconst.lua:130，调用点 streettaskv2panel.lua:151），
         服务端原样存下来；而客户端画标记要求 0 < task_id
         （trackingcommon.lua:33）-> 存档里留下非法值。

  Bug D：服务端**从不自动切换**追踪目标。玩家追踪的主线任务完成后，
         street_trace_taskid 仍指向那个已完成的任务 -> 地图标点停在旧地方，
         玩家得反复点「追踪」。

  伪造 A：Street_Actor_InteractID_Update_CS 的第 4 个参数以前被当成 taskId
         直接完成任意在包任务（含 type0 根）。

  伪造 B：SingleStartMatch_CS 的 copyId / levelId 以前不校验，
         可以凭空把副本上下文设成任意值，绕过 SpaceTask 的副本校验。

用法: python tests/test_story_trace.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_tr_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
    story,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story import seed as SD  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

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


def fresh(name):
    u = "{}_{}".format(name, _TAG)
    msmod._live_init_dict(u)
    st = pstate.get_state(uid=u)
    SD.seed(st, NOW)
    return u, st


def run():
    global _TAG
    _TAG = "{:04x}".format(random.randint(0, 0xFFFF))
    del passed[:]
    del failed[:]

    print()
    print("[1] Bug C：取消追踪哨兵 -1000 要归一化成 0")
    u1, st1 = fresh("tc")
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([-1000]), 1, uid=u1)
    check("★ 发 -1000 后 street_trace_taskid = 0",
          st1.props.get("street_trace_taskid") == 0,
          st1.props.get("street_trace_taskid"))
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u1)
    check("正常正数照常存", st1.props.get("street_trace_taskid") == 1045,
          st1.props.get("street_trace_taskid"))

    print()
    print("[2] Bug D：追踪的任务完成后自动切到下一环")
    u2, st2 = fresh("td")
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1000]), 1, uid=u2)
    check("追踪 1000", st2.props.get("street_trace_taskid") == 1000)
    # 走真实链路完成 1000：StartGame -> 结算
    guarded_handle(rpc_index.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 1004, "storyID": 1}]), 2,
                   uid=u2)
    guarded_handle(rpc_index.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                   msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                   3, uid=u2)
    check("1000 已完成", story.story_state(st2.props, 1000)
          == sm.TASK_STATE_FINISHED, story.story_state(st2.props, 1000))
    check("1003 已启动", story.story_state(st2.props, 1003)
          == sm.TASK_STATE_DOING, story.story_state(st2.props, 1003))
    check("★ 追踪自动从 1000 切到 1003",
          st2.props.get("street_trace_taskid") == 1003,
          st2.props.get("street_trace_taskid"))

    print()
    print("[3] Bug D 反向：完成的不是被追踪的任务时不许乱动")
    u3, st3 = fresh("te")
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 1, uid=u3)
    story.finish_story_task(st3, 1000, NOW)
    check("★ 追踪 1045，完成 1000 后追踪仍是 1045",
          st3.props.get("street_trace_taskid") == 1045,
          st3.props.get("street_trace_taskid"))

    print()
    print("[4] 伪造 A：Street_Actor_InteractID_Update_CS 不能完成 type0")
    u4, st4 = fresh("tf")
    t0 = [t for t in (st4.props.get("story_task_bag") or {})
          if str(t).lstrip("-").isdigit()
          and (gamedata.story_task(int(t)) or {}).get("TaskType") == 0]
    check("包里有 type0 任务", bool(t0), t0[:5])
    tgt = int(sorted(t0)[0])
    before = story.story_state(st4.props, tgt)
    guarded_handle(rpc_index.RPC_INDEX["Street_Actor_InteractID_Update_CS"],
                   msmod.mp_arr([99999, 1, 1, tgt, 1]), 1, uid=u4)
    check("★ 伪造 taskId 完成后 type0 仍是 Doing",
          story.story_state(st4.props, tgt) == before,
          (before, story.story_state(st4.props, tgt)))

    print()
    print("[5] 伪造 B：SingleStartMatch_CS 不能设任意 copyId / levelId")
    u5, st5 = fresh("tg")
    guarded_handle(rpc_index.RPC_INDEX["SingleStartMatch_CS"],
                   msmod.mp_arr([{"copyId": "no_such_copy_xyz",
                                  "levelId": 52021}]), 1, uid=u5)
    check("★ 不存在的 copyId 被拒（上下文没被设）",
          st5.props.get("_oo_cur_copy") is None,
          st5.props.get("_oo_cur_copy"))
    guarded_handle(rpc_index.RPC_INDEX["SingleStartMatch_CS"],
                   msmod.mp_arr([{"copyId": "dgarea_linglongge",
                                  "levelId": 999999}]), 2, uid=u5)
    check("★ rootLevel 外的 levelId 被改成首关",
          st5.props.get("_oo_cur_level") == 52021,
          st5.props.get("_oo_cur_level"))

    print()
    print("[6] 伪造 C：不在副本里结算不推进剧情")
    u6, st6 = fresh("th")
    before6 = story.story_state(st6.props, 1000)
    guarded_handle(rpc_index.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                   msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                   1, uid=u6)
    check("★ 没 StartGame 时 1000 不完成",
          story.story_state(st6.props, 1000) == before6,
          (before6, story.story_state(st6.props, 1000)))

    print()
    print("[7] 老号登录也必须播种（阶段 C 的调用条件 bug）")
    # 实机证据：ootest001 是已有存档，以前 init_new_player 只在 is_new 时调用，
    # 所以老号永远只有 1 个任务。现在改成每次登录都播种（seed 本身幂等）。
    # ★ 关键：必须从「只有 1 个任务的老存档」开始，否则测试是假阳性
    #   （第一次播完 bag 已经是 83，再登录即使不播种也还是 83 ——
    #    这个假阳性是破坏性验证抓出来的，审计也提过同类问题）。
    u7 = "ti_" + _TAG
    msmod._live_init_dict(u7)
    st7 = pstate.get_state(uid=u7)
    # 手工把 bag 削成「只有 1000」+ 清空 done，模拟旧代码留下的存档
    bag7 = st7.props.get("story_task_bag")
    for k in [k for k in bag7 if k not in ("__id__", "__tp__")]:
        bag7.pop(k, None)
    done7 = st7.props.get("done_story_tasks")
    for k in [k for k in done7 if k not in ("__id__", "__tp__")]:
        done7.pop(k, None)
    story.start_story_task(st7, 1000, NOW)
    n7a = sum(1 for k in bag7 if k not in ("__id__", "__tp__"))
    check("模拟老存档：bag 只有 1 个任务", n7a == 1, n7a)
    st7.save()
    # 模拟「老号再次登录」：清缓存 + 重新走 _live_init_dict
    pstate._STATES.clear()
    msmod._live_init_dict(u7)
    st7b = pstate.get_state(uid=u7)
    n7b = sum(1 for k in (st7b.props.get("story_task_bag") or {})
              if k not in ("__id__", "__tp__"))
    check("★ 老号登录时补播到 83（以前只在 is_new 时播种）",
          n7b == 83, n7b)

    print()
    print("[8] 切街区后追踪标点要刷新 / 换到当前街区能做的任务")
    u8, st8 = fresh("tj")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"],
                   msmod.mp_arr([101]), 1, uid=u8)
    check("先进街区 101", st8.props.get("street_id") == 101,
          st8.props.get("street_id"))
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u8)
    check("追踪 1045（GuideSceneId=101）",
          st8.props.get("street_trace_taskid") == 1045,
          st8.props.get("street_trace_taskid"))
    frames8 = guarded_handle(rpc_index.RPC_INDEX["RequestSwitchStreet_CS"],
                             msmod.mp_arr([100]), 3, uid=u8)
    check("切到街区 100", st8.props.get("street_id") == 100,
          st8.props.get("street_id"))
    newt = st8.props.get("street_trace_taskid")
    check("★ 追踪目标被换掉（1045 在 101 做不了）", newt != 1045, newt)
    if newt:
        cfg = gamedata.story_task(newt) or {}
        check("★ 新追踪任务的 GuideSceneId = 100",
              cfg.get("GuideSceneId") == 100, cfg.get("GuideSceneId"))
    # 必须推了 street_trace_taskid 的 OP_CHANGE（否则客户端不刷新标点）
    pushed = False
    for f in frames8:
        if not isinstance(f, (bytes, bytearray)) or len(f) < 6:
            continue
        if int.from_bytes(bytes(f[4:6]), "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        fl = msmod.pb_parse(bytes(f[6:]))
        nm = fl.get(4, [b""])[0]
        if isinstance(nm, (bytes, bytearray)):
            nm = nm.decode("utf-8", "replace")
        if nm == "street_trace_taskid":
            pushed = True
            break
    check("★ 切街区推了 street_trace_taskid 同步帧（客户端靠它刷新）",
          pushed)

    print()
    print("[9] 追踪一个「不在当前街区」的任务 -> 不写进 street_trace_taskid")
    # 客户端 getStreetActorByUniqName 只在当前场景找 NPC（streettasktrackcomp.lua:99），
    # 找不到就 setTrackingInfo(nil) -> 标点停在原地。所以跨街区任务不能写进去。
    u9, st9 = fresh("tk")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"],
                   msmod.mp_arr([100]), 1, uid=u9)
    check("9a 在街区 100", st9.props.get("street_id") == 100,
          st9.props.get("street_id"))
    # 1045 的 GuideSceneId = 101，在街区 100 做不了
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u9)
    check("★ 9b 跨街区任务的 street_trace_taskid 被置 0（不留错误标点）",
          st9.props.get("street_trace_taskid") == 0,
          st9.props.get("street_trace_taskid"))
    check("★ 9c 但玩家意图被记住（_oo_trace_wanted = 1045）",
          st9.props.get("_oo_trace_wanted") == 1045,
          st9.props.get("_oo_trace_wanted"))
    # 走到正确街区 101 -> 自动恢复
    guarded_handle(rpc_index.RPC_INDEX["RequestSwitchStreet_CS"],
                   msmod.mp_arr([101]), 3, uid=u9)
    check("★ 9d 到街区 101 后自动恢复追踪 1045",
          st9.props.get("street_trace_taskid") == 1045,
          st9.props.get("street_trace_taskid"))
    # 同街区追踪正常生效
    u9b, st9b = fresh("tl")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"],
                   msmod.mp_arr([101]), 1, uid=u9b)
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u9b)
    check("★ 9e 同街区追踪照常生效",
          st9b.props.get("street_trace_taskid") == 1045,
          st9b.props.get("street_trace_taskid"))

    print()
    print("=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())