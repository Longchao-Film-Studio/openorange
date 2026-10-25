#!/usr/bin/env python3
"""Soul 连接：心跳 / 可靠属性同步 / 战斗帧回传（P0-E2 第 2 步）。

客户端 soulnet.lua:82-90 用 now - info.reqMs 算 ping，并做 15 秒超时判定，
不回就会断 Soul 连接。onlinebattlespacenetcomponent.lua:138 的
spaceFrameMessage 要求服务端把上行帧回传，战斗逻辑才能走完闭环。

★ 这些消息都走 localSoul:serverMsg(...)，需要 Soul 实体已绑定；
  而 bindSoul 依赖副本入口（Phase 3）。现在先把 RPC 面补全。
"""
from ..rpc_common import decode_params
from .space import _s2c_entity

SOUL_HEARTBEAT_SC = 1531962635

CMD_PROPERTY_RELIABLE_SYNC_SC = 66862165

SYNC_FRAME_FROM_SERVER = 1791975984

def _soul_heartbeat(params, uid=None):
    """soulHeartbeat_CS {{reqMs}, pingInfo} -> soulHeartbeat_SC {reqMs}。

    客户端 soulnet.lua:82-90 用 `now - info.reqMs` 算 ping，
    并在 :82-89 里做 15 秒超时判定 —— 不回就会断 Soul 连接。
    """
    vals = decode_params(params)
    req = 0
    if isinstance(vals, list) and vals:
        first = vals[0]
        if isinstance(first, dict):
            req = int(first.get("reqMs") or 0)
        elif isinstance(first, (int, float)):
            req = int(first)
    return [_s2c_entity("soulHeartbeat_SC", SOUL_HEARTBEAT_SC,
                        [{"reqMs": req}])]

def _reliable_property_sync(params, uid=None):
    """Cmd_Property_ReliableSync_CS {msgId, data} -> Cmd_Property_ReliableSync_SC(msgId)。"""
    vals = decode_params(params)
    msg_id = 0
    if isinstance(vals, list) and vals:
        try:
            msg_id = int(vals[0] or 0)
        except Exception:
            msg_id = 0
    return [_s2c_entity("Cmd_Property_ReliableSync_SC",
                        CMD_PROPERTY_RELIABLE_SYNC_SC, [msg_id])]

def _space_frame_message(params, uid=None):
    """spaceFrameMessage {view2LogicDataCache} -> syncFrameFromServerToClient(帧)。

    单机私服没有别的玩家，把客户端上行的帧原样回传，
    让战斗逻辑走完「上行 -> 服务端确认 -> 下行」的闭环。
    """
    vals = decode_params(params)
    payload = []
    if isinstance(vals, list) and vals:
        payload = vals[0]
    return [_s2c_entity("syncFrameFromServerToClient", SYNC_FRAME_FROM_SERVER,
                        [payload])]
