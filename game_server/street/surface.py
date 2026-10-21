#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阴阳界切换（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:433-448：
  RequestEnterStreetSurface(worldId) -> serverRpc("RequestEnterStreetSurface_CS", {worldId})
  RequestLeaveStreetSurface()        -> serverMsg("RequestLeaveStreetSurface_CS", {})  单向

★ 阴阳界状态读的是 **StreetInfoDict.surface_enter_time**（不是每街区的）：
  avatarstreetcomp.lua:560-562 getCurStreetYinYangEnterTime() -> street_info.surface_enter_time
  而 streetsystemmgrbase.lua:679-685 用它决定 worldIndex（0=阳界 1=阴界）。
"""
import time

from .. import ms as msmod
from ..rpc_common import state, sync_frame
from ..rpc_index import ACK_OK
def _set_surface_time(st, value):
    """改 street_info.surface_enter_time 并推同步。"""
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return []
    si["surface_enter_time"] = int(value)
    st.set("street_info", si)
    return [sync_frame(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, "surface_enter_time", int(value))]

def _enter_surface(rid, params, uid=None):
    """RequestEnterStreetSurface_CS {worldId} —— 进阴界。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    st = state(uid)
    frames.extend(_set_surface_time(st, int(time.time())))
    st.save()
    print("[street] 进入阴界", flush=True)
    return frames

def _leave_surface(params, uid=None):
    """RequestLeaveStreetSurface_CS {} —— 回阳界（单向）。"""
    st = state(uid)
    out = _set_surface_time(st, 0)
    st.save()
    print("[street] 离开阴界", flush=True)
    return out
