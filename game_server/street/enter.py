#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
from .. import ms as msmod
from ..property import tx as _tx
from ..rpc_common import decode_params, state, throttled_save
from ..rpc_index import ACK_OK
from .info import _make_street_info_entry
def _switch_street(rid, params, uid=None):
    """RequestSwitchStreet_CS {streetId, streetParams} / RpcEnterStreet {id, streetParams}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        new_id = int(vals[0])
    except Exception:
        return frames
    return _apply_street_change(frames, new_id, uid)

def _leave_street(rid, params, uid=None):
    """RequestLeaveStreet_CS {id} —— 离开街区，street_id 归 0。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    return _apply_street_change(frames, 0, uid)

def _apply_street_change(frames, new_id, uid):
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
    frames.extend(tx.frames())
    print("[street] %s -> %s" % (old, new_id), flush=True)
    return frames

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
