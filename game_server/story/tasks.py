#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用任务（TaskData 那一套：task_bag / done_tasks）（P0-E2 第 2 步）。

逆向依据 entities/avatarcomponent/avatartaskcomp.lua:
  getTaskState(taskType, taskID) 读 task_bag[taskType][taskID] / done_tasks[taskType][taskID]
  RequestFetchTask_CS {task_id}          接任务
  RequestDoneTask_CS {task_id}           完成任务
  RequestCompleteTask_CS {task_id}       完成任务（另一条路径）
  RequestCompleteUnitlTask_CS {task_id}  完成到指定任务
  RequestDoneSomeTypeTask_CS {type}      完成某类型的全部任务

注意 task_bag 是**两层**：task_bag[taskType] 是 TaskBag（Dict<taskId, TaskItem>），
所以同步要打在 TaskBag 的 __id__ 上，name = taskId。
"""
from .. import ms as msmod
from .. import gamedata
from .. import tasks
from ..property import sync as _sync
from ..rpc_common import SYNC_SELF, decode_params, state, sync_frame
from ..rpc_index import ACK_OK
from .rpc import _story_sync_frames as _story_frames

def _task_sync_frames(st, changes):
    """把 tasks 模块的同步描述转成帧（支持两层嵌套 sub2/del2）。"""
    frames = []
    for ch in changes:
        kind = ch[0]
        if kind == "sync":
            # ★ tasks 模块自己给出 (propertyId, optype, name, value)，
            #   不再由这里猜 —— 之前一律用 OP_CHANGE，新建的槽位/任务项都发错。
            _, pid, optype, name, value = ch
            # ★ 必须剥嵌套 __tp__（审计 P0-2 实锤：TaskBag 槽位的 __tp__
            #   会让客户端把字符串当 TaskItem 解析 -> index a string value 崩）
            if optype != msmod.OP_DEL and value is not None:
                value = _sync._strip_private(value, is_root=False)
            if not isinstance(name, str):
                name = str(name)
            frames.append(sync_frame(
                SYNC_SELF, pid, optype, name,
                None if optype == msmod.OP_DEL else value))
        elif kind in ("sub2", "del2"):
            # 兼容旧格式（story 那边还在用）
            _, container, type_key, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            slot = c.get(type_key)
            if not isinstance(slot, dict):
                continue
            op = msmod.OP_CHANGE if kind == "sub2" else msmod.OP_DEL
            frames.append(sync_frame(
                SYNC_SELF, slot.get("__id__") or 0, op, str(key),
                value if kind == "sub2" else None))
    return frames

def drain_event_task_sync(st, res):
    """把 apply_event 返回的通用任务同步描述编成帧（P1 阶段 N-a3）。

    ★ 每个 apply_event 调用点都必须调它 —— 否则客户端 task_bag 缓存与服务器不一致
      （进度条不动、任务完成后按钮不消失）。有元测试守着：
      tests/test_task_event_sync.py。

    apply_event 内部已经把达到完成条件的通用任务 finish 掉了（见
    story/events.py 第 4 段），所以这里的描述既含进度变化也含完成动作。
    """
    ch = getattr(res, "task_sync", None) or []
    if not ch:
        return []
    return _task_sync_frames(st, ch)


def _task_fetch(rid, params, uid=None):
    """RequestFetchTask_CS {task_id} —— 接任务（放进 task_bag）。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    frames.extend(_task_sync_frames(st, tasks.start_task(st, tid)))
    st.save()
    print("[task] 接任务 %s" % tid, flush=True)
    return frames

def _task_done(rid, params, uid=None):
    """RequestDoneTask_CS / RequestCompleteTask_CS / RequestCompleteUnitlTask_CS。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    frames.extend(_task_sync_frames(st, tasks.finish_task(st, tid)))
    # ★ 阶段 D：通用任务完成后要重扫剧情任务。
    #   实测：跨表前置（PreTaskType=1，6 条）的上游是 TaskData，
    #   StoryTaskData.PostTasks 不会启动它；而 seed_cross_table 以前只在登录时调，
    #   所以这 6 条链（共 14+ 条任务）在玩家做通用任务时永远不动。
    #   链式闸门（TaskType 6）同理 —— events.tick 只在登录和副本结算跑。
    from . import seed as _seed
    try:
        _extra = _seed.rescan(st)
        if _extra:
            frames.extend(_story_frames(st, _extra))
    except Exception as e:
        print("[task] 剧情重扫失败: %s" % e, flush=True)
    st.save()
    print("[task] 完成任务 %s" % tid, flush=True)
    return frames

def _task_done_type(rid, params, uid=None):
    """RequestDoneSomeTypeTask_CS {type} —— 把该类型所有进行中任务标完成。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        ttype = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    for tid in tasks.tasks_of_type(st, ttype):
        frames.extend(_task_sync_frames(st, tasks.finish_task(st, tid)))
    from . import seed as _seed
    try:
        _extra = _seed.rescan(st)
        if _extra:
            frames.extend(_story_frames(st, _extra))
    except Exception as e:
        print("[task] 剧情重扫失败: %s" % e, flush=True)
    st.save()
    print("[task] 完成类型 %s 的全部任务" % ttype, flush=True)
    return frames


# ---------------------------------------------------------------- N-c 头衔 RPC

def _grant_awards(st, cfg):
    """发通用任务 AwardItemList 的奖励，返回 (同步帧, 奖励列表)。"""
    awards = cfg.get("AwardItemList")
    if not isinstance(awards, dict) or not awards:
        return [], []
    try:
        from ..dungeon.reward import _grant_battle_items
        return _grant_battle_items(st, awards)
    except Exception as e:
        print("[task] 发奖失败: %s" % e, flush=True)
        return [], []


def _done_title_task(rid, params, uid=None):
    """RequestDoneTitleTask_CS {title_tasks, titel_vit} —— 领取头衔挑战任务奖励。

    客户端依据 `ui/panel/directortitlev3panel/node_challenge_task_item/
    node_challenge_task_itemgroup.lua:147-171`：
      - 遍历 getDirectorChallengeTasks()（即 task_bag[10]），
        只把 **state == WaitDone(4)** 的 id 放进 title_tasks；
      - 体力假任务（fakeTask）存在时 titel_vit = 1，否则 -1。

    服务端：只处理「在 task_bag[10] 且状态是 WaitDone」的 id，逐个 claim + 发奖。
    titel_vit 对应的体力任务属另一套机制（CommonConstData.TitleLoopTaskCost），
    本阶段不实现（见 N8 的范围说明）。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    raw_ids = vals[0] if isinstance(vals[0], list) else []
    vit = vals[1] if len(vals) > 1 else -1
    st = state(uid)
    ch = []
    got = []
    for raw in raw_ids:
        try:
            tid = int(raw)
        except Exception:
            continue
        cfg = gamedata.task(tid) or {}
        if cfg.get("TaskType") != 10:
            print("[task] 拒绝领奖 %s：不是头衔挑战任务" % tid, flush=True)
            continue
        if tasks.task_state(st.props, tid) != tasks.TASK_STATE_WAIT_DONE:
            print("[task] 拒绝领奖 %s：状态不是 WaitDone（当前 %s）"
                  % (tid, tasks.task_state(st.props, tid)), flush=True)
            continue
        _af, _rl = _grant_awards(st, cfg)
        ch.extend(_af)
        ch.extend(tasks.claim_task(st, tid))
        got.append(tid)
        if _rl:
            print("[task] 领奖 %s -> %s" % (tid, _rl), flush=True)
    if ch:
        frames.extend(_task_sync_frames(st, ch))
    st.save()
    print("[task] 头衔领奖：%s（titel_vit=%s）" % (got, vit), flush=True)
    return frames


def _refresh_title_daily_task(rid, params, uid=None):
    """RequestRefreshTitleDailyTask_CS {type} —— 刷新头衔日常任务。

    客户端依据 `ui/panel/directortitlev3panel/node_daily_task_item/
    node_daily_task_itemgroup.lua:200-206`：参数是 `_cfgData.DailyType`（1 或 2）。
    刷新后客户端会重读 `getDirectorDailyTasks()`（即 task_bag[9]）。

    服务端做法：把该 DailyType 下**已完成**的任务重置回 Doing（重新可做）。
    ⚠️ 已知局限：目前没有「每天只能刷新一次」的限制，理论上可反复刷奖励。
       正确的每日限额要等 N-d（每日重置）一起做，这里先按「能刷新」实现并留痕。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        daily_type = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    # ★ N-d：每天只能刷新 1 次（防反复刷奖励）
    if not tasks.can_refresh_daily(st):
        print("[task] 刷新头衔日常被拒：今天已经刷新过", flush=True)
        return [msmod.build_entity_response(rid, ret={"status": False})]
    tasks.mark_daily_refreshed(st)
    ch = []
    reset = []
    bag = st.props.get("task_bag")
    done = st.props.get("done_tasks")
    # 该 DailyType 下、当前不在包里的任务 -> 重新播
    for tid in tasks._all_task_ids():
        cfg = gamedata.task(tid) or {}
        if cfg.get("TaskType") != 9:
            continue
        try:
            if int(cfg.get("DailyType") or -1) != daily_type:
                continue
        except Exception:
            continue
        cur = tasks.task_state(st.props, tid)
        if cur in (tasks.TASK_STATE_DOING, tasks.TASK_STATE_WAIT_DONE):
            continue
        # 已完成 / 已出包 -> 清掉完成记录，重新播
        # （见函数 docstring 里「可反复刷奖励」的已知局限）
        if cur == tasks.TASK_STATE_FINISHED:
            _d = st.props.get("done_tasks")
            if isinstance(_d, dict):
                _dslot = _d.get(cfg.get("TaskType"))
                if isinstance(_dslot, dict):
                    _dslot.pop(tid, None)
                    st.set("done_tasks", _d)
        ch.extend(tasks.start_task(st, tid))
        reset.append(tid)
    if ch:
        frames.extend(_task_sync_frames(st, ch))
    st.save()
    print("[task] 刷新头衔日常 DailyType=%s -> 重置 %s" % (daily_type, reset),
          flush=True)
    return frames
