#!/usr/bin/env python3
"""街区切换 / 离开 / 位置上报（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:107-128, 651-668：
  enterStreet_CS(id)        -> serverRpc("RpcEnterStreet", {id, streetParams})
  AvatarStreet_SwitchStreet -> serverRpc("RequestSwitchStreet_CS", {streetId, streetParams})
  leaveStreet_CS(id)        -> serverRpc("RequestLeaveStreet_CS", {id})
  updatePlayerPos_CS(pos)   -> serverMsg("RequestUpdateStreetPos_CS", {street_id, pos})

客户端拿到 status=true 后会自己 JumpToStreetStage 加载场景，**但小地图是按服务端
属性取的**（node_map_smallgroup.lua:30 读 getStreetID()），所以必须改 street_id 并推同步。

★ 审计修正：切街区时之前手写 OP_CHANGE 打**新建的** street_info 键，
  客户端 _changeSubProperty 里 oldVal 为 nil -> 整条同步被丢弃。
  表现：切街区后小地图/宝箱/交互状态全不同步。现在交给 Tx，新键自动用 OP_ADD。
"""
from shared.log_tee import get_logger

from ..property import tx as _tx
from ..rpc_common import decode_params, respond_after_sync, state, throttled_save
from .info import _make_street_info_entry

_log = get_logger(__name__)
def _switch_street(rid, params, uid=None):
    """RequestSwitchStreet_CS {streetId, streetParams} / RpcEnterStreet {id, streetParams}。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        new_id = int(vals[0])
    except Exception:
        return respond_after_sync(rid, syncs)
    syncs = _apply_street_change(new_id, uid) or []
    return respond_after_sync(rid, syncs)

def _leave_street(rid, params, uid=None):
    """RequestLeaveStreet_CS {id} —— 离开街区，street_id 归 0。"""
    syncs = _apply_street_change(0, uid) or []
    return respond_after_sync(rid, syncs)

def _refresh_trace_on_street_change(st, new_id, syncs):
    """切街区后刷新追踪标点 —— 统一走 story.trace.apply（唯一入口 R3）。

    历史：这里曾经用 `_oo_trace_wanted` 自己重算，找不到就「随便挑一个本街区正在
    做的任务」，实机日志抓到过：
      `[street] 追踪目标 1044 不在街区 100，切到 400009`
    —— 把标点换到了完全无关的任务上，玩家跟着走必然找不到下一步。

    现在统一由 story.trace 判定：只在「正在做 + 本街区」的任务里挑，一个都没有
    就置 0（客户端 clearAllTrackingSign），绝不再挑无关任务。
    """
    from ..story.trace import apply as _trace_apply
    _trace_apply(st, syncs, "切街区", street_id=new_id)
def _apply_street_change(new_id, uid):
    """改 street_id + 保证 street_info 有条目 + 推同步。

    ★ 2026-10-21 审计修正：这里之前手写 OP_CHANGE 打**新建的** street_info 键，
      客户端 _changeSubProperty 里 oldVal 为 nil -> oldVal:customTypeName()
      索引 nil -> 抛错 -> 整条同步被丢弃。
      表现：切街区后小地图/宝箱/交互状态全不同步。
      现在交给 Tx，新键自动用 OP_ADD。
    """
    st = state(uid)
    old = st.props.get("street_id")
    tx = _tx.Tx(st.props)
    tx.set(("street_id",), new_id)
    if new_id:
        si = st.props.get("street_info")
        if isinstance(si, dict) and not isinstance(si.get(new_id), dict):
            # 新街区：先构造（不插入），让 Tx 判定为 OP_ADD
            tx.set(("street_info", new_id), _make_street_info_entry(st))
    st.save()
    syncs = list(tx.frames())
    # ★ 阶段 P：切街到达目标街 -> 完成 type4 剧情任务（唯一正确路径）。
    #   客户端证据见 docs/storytaskresearch.md §16 / §19：
    #     - 13 条 type4 的 Parameter1 全是「目标街区 id」
    #     - 能验证的 4 条 transferStreet 与 Parameter1 4/4 一致
    #     - 客户端不上报 type4，必须服务端在切街时自己判
    #   实机断点：1045「回龙潮阁复命」Parameter1=[100]，玩家从 101 切到 100
    #   时应该完成它，之后 1004 才能进包 -> actor 40005 -> playTimeline 7。
    try:
        from ..story import street_arrival_tasks
        from ..story.rpc import _finish_story_tasks
        _hits = street_arrival_tasks(st, new_id)
        if _hits:
            syncs.extend(_finish_story_tasks(st, _hits, source="street_arrival") or [])
    except Exception as _e:
        _log.warning("[street] 切街完成 type4 失败: %s" % _e)
    # ★ 切街区后刷新追踪标点（否则标点留在旧街区，玩家找不到下一步）
    _refresh_trace_on_street_change(st, new_id, syncs)
    _log.info("[street] %s -> %s" % (old, new_id))
    return syncs

def _update_street_pos(params, uid=None):
    """RequestUpdateStreetPos_CS {street_id, pos_info} —— 客户端移动时上报位置。

    逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:130-135：
      function AvatarStreetComp:updatePlayerPos_CS(pos_info)
        self:serverMsg("RequestUpdateStreetPos_CS", {self.street_id, pos_info})
      end
    调用点：streetplayeractormovesystem.lua:120 / streetmovecomponent.lua:309 /
            streetactorbase.lua:381

    pos_info 形如 {x=, y=, z=, dir=}（streetsystemmgrbase.lua:561-566 读它恢复位置）。
    服务端存下来，下次登录通过 street_pos_info 下发即可回到原位。
    """
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return
    street_id = vals[0]
    pos = vals[1]
    if not isinstance(pos, dict):
        return
    st = state(uid)
    if int(st.props.get("street_id") or 0) != int(street_id or 0):
        return
    saved = {
        "x": float(pos.get("x") or 0),
        "y": float(pos.get("y") or 0),
        "z": float(pos.get("z") or 0),
        "dir": float(pos.get("dir") or 0),
    }
    st.props["_oo_street_pos"] = saved
    throttled_save(st, uid, force=True)
