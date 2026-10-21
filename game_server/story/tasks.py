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
from .. import tasks
from ..property import sync as _sync
from ..rpc_common import decode_params, state, sync_frame
from ..rpc_index import ACK_OK

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
                "", pid, optype, name,
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
                "", slot.get("__id__") or 0, op, str(key),
                value if kind == "sub2" else None))
    return frames

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
    st.save()
    print("[task] 完成类型 %s 的全部任务" % ttype, flush=True)
    return frames
