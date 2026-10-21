#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AOI 移动与微状态（P0-E2 第 2 步）。

逆向依据 entities/avatarcomponent/avataraoicomp.lua:7：
  CallServer("RpcOnClientMove", moveInfo.x, moveInfo.y, moveInfo.z, moveInfo.dir)
单向，记录玩家位置即可（多人联机时要广播给同场景其他人）。
"""
from ..rpc_common import decode_params, state, throttled_save

def _client_move(params, uid=None):
    """RpcOnClientMove [x, y, z, dir] —— 记录位置。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 3):
        return []
    st = state(uid)
    try:
        saved = {"x": float(vals[0]), "y": float(vals[1]), "z": float(vals[2]),
                 "dir": float(vals[3]) if len(vals) > 3 and vals[3] is not None else 0.0}
    except Exception:
        return []
    st.props["_oo_street_pos"] = saved
    throttled_save(st, uid)
    return []

def _change_micro_state(params, uid=None):
    """RpcChangeMicroState [state] —— 记录微状态（挂机/钓鱼等）。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return []
    st = state(uid)
    try:
        st.props["_oo_micro_state"] = int(vals[0] or 0)
    except Exception:
        pass
    return []
