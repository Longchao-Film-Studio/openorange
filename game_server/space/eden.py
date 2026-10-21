#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伊甸（Eden）（P0-E2 第 2 步）。

逆向依据 entities/avatarcomponent/avataredencomp.lua:30-39：
  enterEden(pos_id)  -> CallServer("RpcEnterEden", pos_id or 0)   单向
  BackToEden()       -> CallServer("RpcBackToEden")               单向
两者最终都是「进 SPACE_EDEN 空间」：服务端改 space_p 并下发
  RpcOnEnterSpace(1000, spaceid, {toPosId=...})
客户端 avatarspacecomp.lua:93-118 的 RpcOnEnterSpace 里会
  JumpToEdenStage("StreetData", 502, ...) 加载伊甸场景。
"""
from .. import ms as msmod
from ..rpc_common import decode_params, state
from ..rpc_index import ACK_OK
from .space import SPACE_EDEN, _enter_space

def _enter_eden(params, uid=None):
    """RpcEnterEden [pos_id] / RpcBackToEden [] —— 进伊甸空间。"""
    vals = decode_params(params)
    pos_id = 0
    if isinstance(vals, list) and vals:
        try:
            pos_id = int(vals[0] or 0)
        except Exception:
            pos_id = 0
    info = {}
    if pos_id:
        info["toPosId"] = pos_id
    return _enter_space([SPACE_EDEN, "", info], uid)

def _eden_chat_history(rid, params, uid=None):
    """RpcGetEdenChatHistory {spaceNo} —— 回一条空聊天记录。

    客户端 avatarspacecomp.lua:152-158 的 RpcOnEdenChatHistory(chatInfo)
    只是把 chatInfo 交给回调，空表即可。
    """
    resp = {"Ret": [True, 0], "ChatInfo": []}
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK), resp=resp)]
    return frames
