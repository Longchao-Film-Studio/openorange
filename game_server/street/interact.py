#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NPC 交互 / 红点 / 追踪任务 / 清理宝箱 actor（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetactorcomp.lua:19-40：
  sendStreetActorHadInteract(actor_id, interact_id, interact_group, cb)
    -> serverRpc("Street_Actor_InteractID_Update_CS",
                  {actor_id, interact_group, interact_id, taskId, interact_id})
  语义：把「这个 NPC 的这个交互做过了」记到服务端，
        若命中剧情任务（getTaskIdByParamInt(interact_id, 2)）则顺便完成它。

交互状态存在 StreetInfo.inter_actor_info（IntDictKeyInt，config.lua:6834）。
红点在 StreetInfoDict.street_redpoint（IntDict，avatarstreetcomp.lua:581-587）。
追踪任务存 street_trace_taskid（avatarstreetcomp.lua:741）。
"""
from .. import gamedata
from .. import ms as msmod
from ..property import ids as _ids
from ..property import tx as _tx
from ..rpc_common import respond_after_sync, SYNC_SELF, decode_params, state, sync_frame
from ..rpc_index import ACK_OK
from ..story import events as _events
from ..story.rpc import _find_story_tasks_by_param, _finish_story_tasks
from ..story.tasks import drain_event_task_sync
from .info import _street_info_of, _street_tx_begin
def _actor_interact(rid, params, uid=None):
    """Street_Actor_InteractID_Update_CS {actor_id, interact_group, interact_id, taskId?, interact_id}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 3):
        return frames
    try:
        actor_id = int(vals[0])
        interact_id = int(vals[2])
    except Exception:
        return frames
    task_id = None
    if len(vals) >= 4:
        try:
            task_id = int(vals[3])
        except Exception:
            task_id = None
    st = state(uid)
    street_id = int(st.props.get("street_id") or 0)
    # ★ 用 Tx：新建的 street_info 条目自动 OP_ADD（审计实锤的崩溃点）
    tx, si, entry, is_new = _street_tx_begin(st, street_id)
    if entry is not None:
        ia = entry.get("inter_actor_info")
        if not isinstance(ia, dict):
            ia = {"__id__": _ids.alloc_id(st.props)}
            entry["inter_actor_info"] = ia
        ia[actor_id] = interact_id
        st.set("street_info", si)
        if not is_new:
            tx.set(("street_info", street_id, "inter_actor_info"), ia)
        frames.extend(tx.frames())
    print("[street] NPC %s 交互 %s" % (actor_id, interact_id), flush=True)
    # ★ P0-D：走事件总线发 StreetInteract（11 条剧情任务靠它推进）。
    #   event_id 用 actor+interact 组合，重复交互不会重复走一遍。
    res = _events.apply_event(
        st, _events.ev_street_interact(actor_id, interact_id,
                                        street_id=street_id))
    frames.extend(drain_event_task_sync(st, res))
    # 命中剧情任务就顺手完成
    if task_id:
        frames.extend(_finish_story_tasks(st, [task_id], param=interact_id,
                                          source="street_interact"))
    else:
        hits = _find_story_tasks_by_param(st, interact_id, 2)
        if hits:
            frames.extend(_finish_story_tasks(st, hits, param=interact_id,
                                          source="street_interact"))
    # ★ P1 阶段 N：`res.hits` 是**事件型**命中（type3/7 + type4 街区任务），
    #   `res.chain_hits` 是**链式**命中（type6）。两者来源不同，必须分开传，
    #   否则白名单会把 type4 挡掉（实机回归实测：1045 完不成）。
    if res.hits:
        frames.extend(_finish_story_tasks(st, list(res.hits), source="event"))
    if res.chain_hits:
        frames.extend(_finish_story_tasks(st, list(res.chain_hits), source="chain"))
    st.save()
    return frames

def _street_redpoint(rid, params, uid=None):
    """Street_Redpoint_Update_CS {key, value} —— 存在 street_info.street_redpoint。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        key = int(vals[0])
        value = int(vals[1])
    except Exception:
        return frames
    st = state(uid)
    # street_redpoint 是 StreetInfoDict 自身的字段（混合类型的「结构体」那一半），
    # 所以 propertyId 是 street_info 的 __id__，name 是字段名。
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return frames
    rp = si.get("street_redpoint")
    if not isinstance(rp, dict):
        rp = {"__id__": _ids.alloc_id(st.props)}
        si["street_redpoint"] = rp
    rp[key] = value
    st.set("street_info", si)
    st.save()
    tx = _tx.Tx(st.props)
    tx.set(("street_info", "street_redpoint"), rp)
    frames.extend(tx.frames())
    return frames

def _effective_trace(st, wanted):
    """玩家想追踪 wanted，但只有在当前街区做得了时才真正生效。

    判据：任务的 GuideSceneId（客户端也在用它判断「要不要显示前往提示」，
    见 node_task_pos/node_task_posgroup.lua:342-354）。
      - 任务没有 GuideSceneId（type0/6/7 大多没有）-> 无法判断，照常生效
      - GuideSceneId == 当前 street_id -> 生效
      - 否则 -> 0（客户端会 clearAllTrackingSign，不留错误标点）
    """
    try:
        wanted = int(wanted or 0)
    except Exception:
        return 0
    if wanted <= 0:
        return 0
    cfg = gamedata.story_task(wanted) or {}
    gs = cfg.get("GuideSceneId")
    if gs is None:
        return wanted
    try:
        gs = int(gs)
    except Exception:
        return wanted
    try:
        cur = int(st.props.get("street_id") or 0)
    except Exception:
        cur = 0
    return wanted if gs == cur else 0


def _street_trace_task(rid, params, uid=None):
    """updateStreetTraceTask_CS {trackingTaskID} —— 存追踪中的任务 id。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        tid = int(vals[0] or 0)
    except Exception:
        tid = 0
    # ★ P1 阶段 N（实机反馈 Bug C）：客户端「取消追踪」发的是
    #   SystemConst.StreetIllegalStreetID = -1000（systemconst.lua:130，
    #   调用点 streettaskv2panel.lua:151）。以前原样存下来，
    #   而客户端画标记要求 0 < task_id（trackingcommon.lua:33）->
    #   负数被当成「不追踪」，但存档里留下一个非法值，语义混乱。
    #   统一归一化成 0（= 不追踪）。
    if tid < 0:
        tid = 0
    st = state(uid)
    # ★ P1 阶段 P（实机反馈）：追踪一个「不在当前街区」的任务会让标点停在原地。
    #
    #   客户端证据（ui/panel/streetv2panel/streettasktrackcomp.lua:90,99-106）：
    #     local actor = self:getStreetActorByUniqName(targetActor)
    #     stage:getStageLogic():getActorByUniqName(uniqName)   -- 只在当前场景找
    #     actorTrackSign:setTrackingInfo(InStreetOrEden, actor, storyTask)
    #   目标 NPC 不在当前场景 -> actor = nil -> 标点停在上一次的位置不动。
    #
    #   所以：**记住玩家想追踪什么**（_oo_trace_wanted），但只有该任务的
    #   GuideSceneId 等于当前街区（或任务没有 GuideSceneId）时才真正写进
    #   street_trace_taskid；否则写 0 让客户端 clearAllTrackingSign，
    #   避免留下一个错误位置的标点。玩家走到正确街区后会自动恢复。
    st.props["_oo_trace_wanted"] = tid
    eff = _effective_trace(st, tid)
    st.props["street_trace_taskid"] = eff
    if eff != tid and tid:
        print("[street] 追踪 %s 不在街区 %s，暂不显示标点（到对应街区自动恢复）"
              % (tid, st.props.get("street_id")), flush=True)
    st.save()
    syncs.append(sync_frame(
        SYNC_SELF, 0, msmod.OP_CHANGE, "street_trace_taskid", eff))
    return respond_after_sync(rid, syncs)

def _clear_treasure_actor(params, uid=None):
    """RequestClearStreetTreasureActor_CS {streetID} —— 单向，清掉该街区的宝箱 actor。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return []
    try:
        street_id = int(vals[0])
    except Exception:
        return []
    st = state(uid)
    si, entry = _street_info_of(st, street_id)
    if entry is not None:
        tb = entry.get("treasure_box_info")
        if isinstance(tb, dict):
            for k in [k for k in tb if isinstance(k, int)]:
                del tb[k]
            st.set("street_info", si)
    print("[street] 清理街区 %s 的宝箱 actor" % street_id, flush=True)
    return []
