#!/usr/bin/env python3
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
from shared.log_tee import get_logger

from ..property import ids as _ids
from ..property import tx as _tx
from ..rpc_common import decode_params, respond_after_sync, state
from ..story import events as _events
from ..story.rpc import _find_story_tasks_by_param, finish_story_tasks
from ..story.tasks import drain_event_task_sync
from ..write_guard import mutable
from .info import _street_info_of, _street_tx_begin

_log = get_logger(__name__)
def _actor_interact(rid, params, uid=None):
    """Street_Actor_InteractID_Update_CS {actor_id, interact_group, interact_id, taskId?, interact_id}。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 3):
        return respond_after_sync(rid, syncs)
    try:
        actor_id = int(vals[0])
        interact_id = int(vals[2])
    except Exception:
        return respond_after_sync(rid, syncs)
    task_id = None
    if len(vals) >= 4:
        try:
            task_id = int(vals[3])
        except Exception:
            task_id = None
    st = state(uid)
    street_id = int(st.props.get("street_id") or 0)
    # ★ 用 Tx：新建的 street_info 条目自动 OP_ADD（审计实锤的崩溃点）
    tx, _si, entry, is_new = _street_tx_begin(st, street_id)
    if entry is not None:
        # ★★ 2026-11-17 实机事故就在这里：以前是 ia[actor_id] = interact_id
        #   直接改 props 树里的容器 -> 守卫抛 PropsWriteError -> gate 关连接
        #   -> 客户端 lose server -> 交互上报丢失 -> 主线 1007 永远 Doing。
        #   现在一律「取纯副本 -> 改副本 -> Tx 落地」。
        new_entry = mutable(entry)
        ia = new_entry.get("inter_actor_info")
        if not isinstance(ia, dict):
            ia = {"__id__": _ids.alloc_id(st.props)}
        ia = mutable(ia)
        ia[actor_id] = interact_id
        new_entry["inter_actor_info"] = ia
        if is_new:
            tx.set(("street_info", street_id), new_entry)
        else:
            tx.set(("street_info", street_id, "inter_actor_info"), ia)
        syncs.extend(tx.frames())
    _log.info(f"[street] NPC {actor_id} 交互 {interact_id}")
    # ★ P0-D：走事件总线发 StreetInteract（11 条剧情任务靠它推进）。
    #   event_id 用 actor+interact 组合，重复交互不会重复走一遍。
    res = _events.apply_event(
        st, _events.ev_street_interact(actor_id, interact_id,
                                        street_id=street_id))
    syncs.extend(drain_event_task_sync(st, res))
    # 命中剧情任务就顺手完成
    if task_id:
        syncs.extend(finish_story_tasks(st, [task_id], param=interact_id,
                                          source="street_interact"))
    else:
        hits = _find_story_tasks_by_param(st, interact_id, 2)
        if hits:
            syncs.extend(finish_story_tasks(st, hits, param=interact_id,
                                          source="street_interact"))
    # ★ P1 阶段 N：`res.hits` 是**事件型**命中（type3/7 + type4 街区任务），
    #   `res.chain_hits` 是**链式**命中（type6）。两者来源不同，必须分开传，
    #   否则白名单会把 type4 挡掉（实机回归实测：1045 完不成）。
    if res.hits:
        syncs.extend(finish_story_tasks(st, list(res.hits), source="event"))
    if res.chain_hits:
        syncs.extend(finish_story_tasks(st, list(res.chain_hits), source="chain"))
    st.save()
    return respond_after_sync(rid, syncs)

def _street_redpoint(rid, params, uid=None):
    """Street_Redpoint_Update_CS {key, value} —— 存在 street_info.street_redpoint。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, syncs)
    try:
        key = int(vals[0])
        value = int(vals[1])
    except Exception:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    # street_redpoint 是 StreetInfoDict 自身的字段（混合类型的「结构体」那一半），
    # 所以 propertyId 是 street_info 的 __id__，name 是字段名。
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return respond_after_sync(rid, syncs)
    rp = si.get("street_redpoint")
    if not isinstance(rp, dict):
        rp = {"__id__": _ids.alloc_id(st.props)}
    # 副本 -> Tx 落地（裸写会让守卫抛，连接被打断）
    new_rp = mutable(rp)
    new_rp[key] = value
    tx = _tx.Tx(st.props)
    tx.set(("street_info", "street_redpoint"), new_rp)
    st.save()
    syncs.extend(tx.frames())
    return respond_after_sync(rid, syncs)

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
    tid = max(tid, 0)
    st = state(uid)
    # ★ 统一走 story.trace（唯一入口 R3，2026-10-25）：
    #   玩家「想追踪什么」记进 _oo_trace_wanted（可以是别的街区的任务，走到那儿自动恢复），
    #   真正下发的 street_trace_taskid 由 trace.apply 判定——它保证只指向
    #   「正在做 + 本街区」的任务。取消追踪（客户端发 -1000）走 force_zero。
    from ..story.trace import apply as _trace_apply
    from ..story.trace import note_wanted
    if tid <= 0:
        _trace_apply(st, syncs, "取消追踪", force_zero=True)
    else:
        note_wanted(st, tid)
        _trace_apply(st, syncs, "手动追踪", mode="manual", manual_task=tid)
    st.save()
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
        # ★ 只有条目真的在 props 里才落盘：_street_info_of 现在可能返回
        #   「尚未插入的副本」，那种情况下没有东西可清（也不该凭空建条目）。
        if isinstance(tb, dict) and isinstance(si, dict) and isinstance(si.get(street_id), dict):
            new_tb = mutable(tb)
            for k in [k for k in new_tb if isinstance(k, int)]:
                del new_tb[k]
            tx = _tx.Tx(st.props)
            tx.set(("street_info", street_id, "treasure_box_info"), new_tb)
            st.save()
    _log.info(f"[street] 清理街区 {street_id} 的宝箱 actor")
    return []
