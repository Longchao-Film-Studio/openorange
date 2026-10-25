#!/usr/bin/env python
"""P1 阶段 J：TaskType=0 的完成路径（SpaceTask 驱动）。

★ 这个阶段推翻了阶段 E 的结论

  阶段 E 结论：「type0 无可判定完成条件，270 条阻塞」。
  那个结论**只看 StoryTaskData**，漏了 SpaceTaskData 这一层。

★ 实测证据链（全部一手）

  1. SpaceTaskData 909 条，嵌套 {dungeonId: {taskId: row}}，
     taskId **跨 58 个副本全局唯一**（0 重复）。
  2. 每行有 avatarTask 字段 = 剧情任务 id。
     实测：**270 条 type0 全部被某个 SpaceTask.avatarTask 指向**（100%），
     而其它 TaskType 只有 1 条（type5）被指向 —— 说明这个字段就是为 type0 设计的。
  3. battlecontainer/base/player.lua:416-425 finishSpaceTask：
       有 avatarTask 就 startSyncAvatarTask，最终发
       FinishSpaceTask_Directly_CS {taskId} 给游戏服（带 5 次重试）。
  4. battle/public/system/task/spacetasklogicdatamgr.lua:47-52：
     有 avatarTask 的 SpaceTask 把 (avatarTaskType, avatarTask) 放进
     sync_space_task_ids —— 即「战斗内完成 -> 同步到剧情任务」。
  5. 客户端对 type0 **没有任何**完成上报调用点（阶段 E 实测），
     所以 type0 只可能是被 SpaceTask 反推完成的。

★ 不变量

  1. FinishSpaceTask_Directly_CS {spaceTaskId} 只完成
     SpaceTaskData[spaceTaskId].avatarTask 指向的那一个剧情任务。
  2. 剧情任务必须在 story_task_bag 里才允许完成（防伪造）。
  3. 没有 avatarTask 的 SpaceTask 不改剧情状态。
  4. 走**同一条** finish_story_tasks 落状态，不另拼帧。

用法: python tests/test_story_spacetask.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_st_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
    story,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story import events as SEV  # noqa: E402
from game_server.story import seed as SD  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
NOW = 1700000000
_TAG = ""
RPC = "FinishSpaceTask_Directly_CS"


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


def space_flat():
    """摊平 SpaceTaskData -> {taskId: row}。"""
    raw = gamedata.table("SpaceTaskData") or {}
    out = {}
    for group in raw.values():
        if not isinstance(group, dict):
            continue
        for tid, row in group.items():
            if isinstance(row, dict):
                out[int(tid)] = row
    return out


def run():
    global _TAG
    _TAG = f"{random.randint(0, 0xFFFF):04x}"
    del passed[:]
    del failed[:]

    S = story_table()
    SP = space_flat()
    t0 = sorted(t for t, v in S.items() if v.get("TaskType") == 0)

    print("\n[1] 数据事实：SpaceTask.avatarTask 覆盖 type0")
    check("SpaceTaskData 摊平后 909 条", len(SP) == 909, len(SP))
    check("type0 共 270 条", len(t0) == 270, len(t0))
    pointed = [t for t in t0 if gamedata.space_task_of_avatar(t) is not None]
    check("★ 270 条 type0 全部被 SpaceTask.avatarTask 指向", len(pointed) == 270,
          len(pointed))
    import collections
    tt = collections.Counter(S[t].get("TaskType")
                             for t in S
                             if gamedata.space_task_of_avatar(t) is not None)
    check("★ 被指向的任务里 type0 是 270 条（其它只有 type5 一条）",
          tt.get(0) == 270 and sum(tt.values()) == 271, dict(tt))

    print("\n[2] space_task_avatar_task 映射正确")
    check("★ SpaceTask 10101 -> 剧情任务 1102",
          story.space_task_avatar_task(10101) == 1102,
          story.space_task_avatar_task(10101))
    check("★ SpaceTask 10102 -> 剧情任务 1103",
          story.space_task_avatar_task(10102) == 1103,
          story.space_task_avatar_task(10102))
    check("未知 SpaceTask 返回 None",
          story.space_task_avatar_task(99999999) is None)
    bad = []
    for sid, row in SP.items():
        a = row.get("avatarTask")
        if a is None:
            continue
        if story.space_task_avatar_task(sid) != int(a):
            bad.append(sid)
    check("★ 909 条 SpaceTask 的映射全部正确", not bad, bad[:5])

    print("\n[3] RPC 已注册")
    check("★ FinishSpaceTask_Directly_CS 在 RPC_INDEX 里",
          rpc_index.RPC_INDEX.get(RPC) == 964136682,
          rpc_index.RPC_INDEX.get(RPC))

    print("\n[4] 真实链路：推进到 1102 -> SpaceTask 完成 -> 1103 启动")
    uid = "st_" + _TAG
    msmod.live_init_dict(uid)
    st = pstate.get_state(uid=uid)
    SD.seed(st, NOW)
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
    SEV.apply_event(st, SEV.ev_dungeon_finish(1004, 1, 1))
    story.finish_story_task(st, 1000, NOW)
    for tid in chain[1:]:
        if tid not in S or S[tid].get("TaskType") == 0:
            break
        t = S[tid].get("TaskType")
        if t in (1, 2, 5):
            story.finish_story_task(st, tid, NOW)
        elif t == 6:
            story.mark_story_task_finished(st, tid, NOW)
            story.archive_story_task(st, tid, NOW)
        elif t == 4:
            # ★ 阶段 P：type4 完成条件 = 切街到 Parameter1[0]（不再靠交互 NPC）
            p1 = S[tid].get("Parameter1") or []
            want = int(p1[0]) if p1 else None
            if tid in story.street_arrival_tasks(st, want):
                story.finish_story_task(st, tid, NOW)
            else:
                break
    check("★ 1101 已完成", story.story_state(st.props, 1101)
          == sm.TASK_STATE_FINISHED, story.story_state(st.props, 1101))
    check("★ 1102 已自动进包且是 Doing(3)",
          1102 in (st.props.get("story_task_bag") or {})
          and story.story_state(st.props, 1102) == sm.TASK_STATE_DOING,
          story.story_state(st.props, 1102))

    # ★ 必须先进副本：SpaceTask 10101 的 dungeonId=dgarea_linglongge,
    #   levelId=52021，服务端会校验 _oo_cur_copy / _oo_cur_level。
    #   MainlineData[10103].dungeonid = dgarea_linglongge，rootLevel[0] = 52021
    guarded_handle(rpc_index.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 3, "dungeonId": 10103}]), 9,
                   uid=uid)
    check("★ 进副本后 _oo_cur_copy = dgarea_linglongge",
          st.props.get("_oo_cur_copy") == "dgarea_linglongge",
          st.props.get("_oo_cur_copy"))
    check("★ 进副本后 _oo_cur_level = 52021（= SpaceTask 要求的关卡）",
          st.props.get("_oo_cur_level") == 52021,
          st.props.get("_oo_cur_level"))
    pre = pre_snapshot(uid)
    frames = guarded_handle(rpc_index.RPC_INDEX[RPC],
                            msmod.mp_arr([10101]), 1, uid=uid)
    check("★ 走 FinishSpaceTask_Directly_CS 后 1102 -> Finished(6)",
          story.story_state(st.props, 1102) == sm.TASK_STATE_FINISHED,
          story.story_state(st.props, 1102))
    check("★ 1102 归档进 done_story_tasks",
          1102 in (st.props.get("done_story_tasks") or {}))
    check("★ PostTasks 自动启动 1103（Doing）",
          story.story_state(st.props, 1103) == sm.TASK_STATE_DOING,
          story.story_state(st.props, 1103))
    sc, errs = frames_safe(pre, frames, "spacetask")
    check("帧在客户端模型里不崩且无警告",
          not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[5] 防伪造：不在副本里 / 不在包里的请求都要被拒")
    # 5a 不在副本里（_oo_cur_copy 为 None）-> 拒
    uid_d = "std_" + _TAG
    msmod.live_init_dict(uid_d)
    st_d = pstate.get_state(uid=uid_d)
    SD.seed(st_d, NOW)
    story.start_story_task(st_d, 1102, NOW)
    check("5a 1102 已在包里但玩家不在副本",
          1102 in (st_d.props.get("story_task_bag") or {})
          and st_d.props.get("_oo_cur_copy") is None)
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([10101]), 8, uid=uid_d)
    check("★ 不在副本里发 FinishSpaceTask 被拒（1102 未完成）",
          story.story_state(st_d.props, 1102) != sm.TASK_STATE_FINISHED,
          story.story_state(st_d.props, 1102))
    # 5b 进了错的副本 -> 拒
    guarded_handle(rpc_index.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 3, "dungeonId": 10101}]), 9,
                   uid=uid_d)
    check("5b 进了 dgarea_lcmansion（错副本）",
          st_d.props.get("_oo_cur_copy") == "dgarea_lcmansion",
          st_d.props.get("_oo_cur_copy"))
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([10101]), 8, uid=uid_d)
    check("★ 错副本里发 FinishSpaceTask 被拒（1102 未完成）",
          story.story_state(st_d.props, 1102) != sm.TASK_STATE_FINISHED,
          story.story_state(st_d.props, 1102))
    uid2 = "st2_" + _TAG
    msmod.live_init_dict(uid2)
    st2 = pstate.get_state(uid=uid2)
    check("未播种时 1102 不在包里",
          1102 not in (st2.props.get("story_task_bag") or {}))
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([10101]), 2, uid=uid2)
    check("★ 伪造 SpaceTask 完成请求被拒绝（1102 仍不在 done）",
          1102 not in (st2.props.get("done_story_tasks") or {}))
    check("★ 1102 状态仍是 -1", story.story_state(st2.props, 1102) == -1,
          story.story_state(st2.props, 1102))

    print("\n[6] 没有 avatarTask 的 SpaceTask 不改剧情状态")
    no_avatar = [sid for sid, row in SP.items() if row.get("avatarTask") is None]
    check("存在没有 avatarTask 的 SpaceTask", bool(no_avatar), len(no_avatar))
    uid3 = "st3_" + _TAG
    msmod.live_init_dict(uid3)
    st3 = pstate.get_state(uid=uid3)
    SD.seed(st3, NOW)
    before = len([k for k in (st3.props.get("done_story_tasks") or {})
                  if k not in ("__id__", "__tp__")])
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([no_avatar[0]]), 3,
                   uid=uid3)
    after = len([k for k in (st3.props.get("done_story_tasks") or {})
                 if k not in ("__id__", "__tp__")])
    check("★ 无 avatarTask 的 SpaceTask 不产生任何剧情完成", before == after,
          (before, after))

    print("\n[6.5] 防伪造：断线重连后副本上下文必须清空")
    uid_r = "str_" + _TAG
    msmod.live_init_dict(uid_r)
    st_r = pstate.get_state(uid=uid_r)
    SD.seed(st_r, NOW)
    story.start_story_task(st_r, 1102, NOW)
    guarded_handle(rpc_index.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 3, "dungeonId": 10103}]), 11,
                   uid=uid_r)
    check("6.5a 进副本后 cur_copy 已设",
          st_r.props.get("_oo_cur_copy") == "dgarea_linglongge",
          st_r.props.get("_oo_cur_copy"))
    st_r.save()
    # 模拟断线重连：清内存缓存 + 重新登录
    pstate.STATES.clear()
    msmod.live_init_dict(uid_r)
    st_r2 = pstate.get_state(uid=uid_r)
    check("★ 6.5b 重连后 _oo_cur_copy / _oo_cur_level 被清空",
          st_r2.props.get("_oo_cur_copy") is None
          and st_r2.props.get("_oo_cur_level") is None,
          (st_r2.props.get("_oo_cur_copy"), st_r2.props.get("_oo_cur_level")))
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([10101]), 12, uid=uid_r)
    check("★ 6.5c 重连后发 FinishSpaceTask 被拒（1102 未完成）",
          story.story_state(st_r2.props, 1102) != sm.TASK_STATE_FINISHED,
          story.story_state(st_r2.props, 1102))

    print("\n[7] 端到端：进副本 -> SpaceTask -> 剧情推进 -> 结算")
    uid4 = "st4_" + _TAG
    msmod.live_init_dict(uid4)
    st4 = pstate.get_state(uid=uid4)
    SD.seed(st4, NOW)
    # 直接给 1102 建 Doing 条目（省掉主线推进，本用例只验副本内链路）
    story.start_story_task(st4, 1102, NOW)
    # 1) 进副本 dgarea_linglongge（MainlineData[10103].dungeonid）
    guarded_handle(rpc_index.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"system_type": 3, "dungeonId": 10103}]), 7,
                   uid=uid4)
    check("★ 进副本后 cur_copy/cur_level/cur_area 都对",
          (st4.props.get("_oo_cur_copy") == "dgarea_linglongge"
           and st4.props.get("_oo_cur_level") == 52021
           and st4.props.get("_oo_cur_area") == 10103),
          (st4.props.get("_oo_cur_copy"), st4.props.get("_oo_cur_level"),
           st4.props.get("_oo_cur_area")))
    # 2) SpaceTask 完成 -> 剧情任务完成
    guarded_handle(rpc_index.RPC_INDEX[RPC], msmod.mp_arr([10101]), 8, uid=uid4)
    check("★ 副本内 SpaceTask 完成 -> 1102 Finished",
          story.story_state(st4.props, 1102) == sm.TASK_STATE_FINISHED,
          story.story_state(st4.props, 1102))
    # 3) 结算 -> 临时字段清理
    guarded_handle(rpc_index.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                   msmod.mp_arr([{"typeId": 3, "modeId": 1, "level": 1}]), 9,
                   uid=uid4)
    check("★ 结算后 _oo_cur_copy / _oo_cur_area 被清掉",
          st4.props.get("_oo_cur_copy") is None
          and st4.props.get("_oo_cur_area") is None,
          (st4.props.get("_oo_cur_copy"), st4.props.get("_oo_cur_area")))
    check("★ 结算不影响已完成状态",
          story.story_state(st4.props, 1102) == sm.TASK_STATE_FINISHED,
          story.story_state(st4.props, 1102))

    print("\n[8] 覆盖率工具按新口径算")
    import importlib.util
    scp = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "story_coverage.py")
    spec = importlib.util.spec_from_file_location("scj", scp)
    scm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scm)
    T = {int(k): v for k, v in (gamedata.table("TaskData") or {}).items()
         if isinstance(v, dict) and v.get("uiD") is not None}
    E = {int(k): v for k, v in (gamedata.table("TaskEventData") or {}).items()
         if isinstance(v, dict) and v.get("uiD") is not None}
    spmap = {}
    for row in SP.values():
        a = row.get("avatarTask")
        if a is not None:
            spmap.setdefault(int(a), row)
    ok, why = scm.completable(S, T, E, 1102, spmap)
    check("★ 覆盖率工具把 1102 算成可完成", ok, why)
    reach = scm.bfs(scm.roots_of(S), S)
    n_ok = len([t for t in reach if scm.completable(S, T, E, t, spmap)[0]])
    pct = 100.0 * n_ok / len(reach)
    check("★ 可完成数 >= 950（阶段 J 前是 685）", n_ok >= 950, n_ok)
    check("★ 覆盖率 >= 92%", pct >= 92.0, round(pct, 1))
    print(f'      可达 {len(reach):d}，可完成 {n_ok:d}（{pct:.1f}%）')

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
