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
from ..rpc_common import respond_after_sync, SYNC_SELF, state, sync_frame

from shared.log_tee import get_logger
_log = get_logger(__name__)
def _set_surface_time(st, value):
    """改 street_info.surface_enter_time 并推同步。"""
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return []
    si["surface_enter_time"] = int(value)
    st.set("street_info", si)
    return [sync_frame(
        SYNC_SELF, si.get("__id__") or 0, msmod.OP_CHANGE, "surface_enter_time", int(value))]

def _enter_surface(rid, params, uid=None):
    """RequestEnterStreetSurface_CS {worldId} —— 进阴界 / 切昼夜。

    ★★ 不变量（R1，不许改顺序）：属性同步帧必须在应答帧【之前】。

    依据（客户端，逐条可指）：
      1. core/client/gateclientrpchandler.lua:entityMessageResponse
         收到应答帧就【同步】执行回调：SafeCallback(callback, retStatus, response)。
      2. system/streetsystem/manager/streetsystemmgrbase.lua:tryChangeWorld
         回调里立刻读 getCurStreetYinYangEnterTime() 判定：
           local nextWorldIndex = self.worldIndex == 0 and 1 or 0
           local serverWorld    = getCurStreetYinYangEnterTime() == 0 and 0 or 1
           if nextWorldIndex ~= serverWorld then
             error("服务器和客户端数据不同步")   -- 然后 return
      3. entities/avatarcomponent/avatarstreetcomp.lua:560-562
         getCurStreetYinYangEnterTime() = self.street_info.surface_enter_time or 0

    若应答帧在前：回调读到的是【旧】surface_enter_time -> 判定不等 -> error
    -> switchWorld 不执行、昼夜不切、对话队列中断（任务也推进不了）。
    2026-10-23「铁窗泪」实测：10101 播完切天气失败 -> 重播一遍 -> 第二次才通过。
    """
    st = state(uid)
    syncs = _set_surface_time(st, int(time.time()))
    st.save()
    _log.info("[street] 进入阴界")
    return respond_after_sync(rid, syncs)

def _leave_surface(params, uid=None):
    """RequestLeaveStreetSurface_CS {} —— 回阳界（单向）。"""
    st = state(uid)
    out = _set_surface_time(st, 0)
    st.save()
    _log.info("[street] 离开阴界")
    return out
