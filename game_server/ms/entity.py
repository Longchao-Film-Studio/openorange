#!/usr/bin/env python3
"""实体帧：应答构造 / 解析 / S2C 消息（P0-E3 从 ms.py 拆出）。

★ 应答不是独立帧类型，而是 entityMessage(type=2) 带 methodName=__rpc_response__。
  依据：client_gate.proto 的 IGateClientService 只有
  1=connectResponse 2=entityMessage 3=channelMessage 4=createChannelEntity 5=disconnectClient，
  没有 entityMessageResponse；而 xlua.dll 镜像里 GateClientProtobufHandler 的
  字符串表有 __rpc_response__ 这个魔数。
"""
from .frames import frame
from .protocol import (mp_arr, mp_decode, pb_bytes_field, pb_parse,
                       pb_varint_field)
# server -> client 的应答帧类型。
# ★ 逆向确定：应答不是独立帧类型，而是 entityMessage(type=2) 带 methodName=__rpc_response__。
#   依据：client_gate.proto 的 IGateClientService 只有
#   1=connectResponse 2=entityMessage 3=channelMessage 4=createChannelEntity 5=disconnectClient ...
#   没有 entityMessageResponse；而 xlua.dll 镜像里 GateClientProtobufHandler 的
#   字符串表有 __rpc_response__ 这个魔数。
ENTITY_RESPONSE_FRAME_TYPE = 2

def parse_entity_response(pkt):
    """解析服务端应答帧，返回 (mtype, methodName, rid, params)。

    ★ 逆向确定的应答格式（见 build_entity_response 注释）：
      frame type=2 (entityMessage)
        field 2 = \"__rpc_response__\"
        field 4 = msgpack [retStatus, response]
        field 5 = rid (int64)
    """
    mtype = int.from_bytes(pkt[4:6], "little")
    body = pkt[6:]
    i = 0
    n = len(body)
    method = None
    params = None
    rid = 0
    while i < n:
        key = body[i]
        i += 1
        fn, wt = key >> 3, key & 7
        if wt == 0:
            val = 0
            shift = 0
            while True:
                b = body[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        elif wt == 2:
            ln = 0
            shift = 0
            while True:
                b = body[i]
                i += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            val = body[i:i + ln]
            i += ln
        else:
            raise ValueError("unsupported wire type %d" % wt)
        if fn == 2:
            method = val.decode("utf-8", "replace")
        elif fn == 4:
            decoded = mp_decode(val)
            params = decoded[0] if isinstance(decoded, tuple) else decoded
        elif fn == 5:
            rid = val
    return mtype, method, rid, params

def build_entity_response(rid, ret=None, resp=None):
    """构造 serverRpc（带回调）的应答帧。

    ★ 应答**不是** entityMessage，而是独立的 gate 方法 entityMessageResponse。
    依据 core/client/gateclientrpchandler.lua:128-150（实机源码）：
        function GateClientRpcHandler:entityMessageResponse(rid, parameters)
          local name, callback = ClientRepo.responseManager:popResponseCallback(rid)
          if callback then
            local retStatus, response = unpack(parameters)
            SafeCallback(callback, retStatus, response)
          end
        end
    也就是说：
      帧类型 = 3
        （1=connectResponse，2=entityMessage，4=createChannelEntity，
          3 是夹在 entityMessage 和 createChannelEntity 之间的 entityMessageResponse）
      字段 1 = rid（int）
      字段 2 = msgpack 参数，内容就是 [retStatus, response] 两元组
    这条路径上不需要方法名，所以也不需要在客户端注册 _SC 名字。

    反证：把应答塞进 entityMessage 时，客户端日志报
      [RpcCall] Entity(avatar_2000) CallRpc Error. Function(RpcGetSunActLevel_SC) Is Not Rpc.
    因为 entityMessage 一律走 CallRpc 查 EntityFactory.rpc_methods。
    """
    if ret is None:
        ret = {"status": True, "errmsg": ""}
    if resp is None:
        resp = {}
    payload = mp_arr([ret, resp])
    # ★★★ 2026-10-20 逆向确定（扫客户端进程内存拿到解密后的 proto 描述符）：
    #
    #   client_gate.proto 里 IGateClientService 的方法顺序是：
    #     1=connectResponse 2=entityMessage 3=channelMessage 4=createChannelEntity
    #     5=disconnectClient 6=bindAccount 7=bindAvatar 8=bindSoul 9=unbindSoul ...
    #   **根本没有 entityMessageResponse 这个帧类型**！
    #
    #   EntityMessage 的字段（描述符二进制直接读出来的）：
    #     entityId=1  methodName=2  index=3  parameters=4  rid=5(int64)
    #     sessionId=6  context=7
    #
    #   xlua.dll 镜像里 GateClientProtobufHandler 的字符串表：
    #     connectResponse / __rpc_response__ / entityMessageResponse / entityMessage / ...
    #   -> 应答就是 entityMessage(type=2)，但 methodName 必须是 __rpc_response__，
    #      客户端看到这个魔数就改调 entityMessageResponse(rid, parameters)。
    #
    #   之前所有失败的原因：methodName 留空 -> 被派发到 Lua 的 entityMessage，
    #   报 'got empty methodName'，回调永远等不到。
    body = (pb_bytes_field(2, b"__rpc_response__")
            + pb_bytes_field(4, payload)
            + pb_varint_field(5, int(rid)))
    return frame(2, body)
    #   证据：客户端日志一直有
    #     GateClientRpcHandler.lua:196: attempt to index local 'chanMsg' (a nil value)
    #   而 channelMessage(chanMsg) 在 gateclientrpchandler.lua:165 ——
    #   说明我们发的 type=3 帧被派发给了 channelMessage，chanMsg 解不出来就是 nil。
    #   更硬的证据：把应答发成 type=3 之后，客户端的 _SC 回调全部超时：
    #     AvatarUserDataComp:SetSignature_SC, retStatus: {status=false, errmsg="rpc timeout"}
    #   服务端明明 TX 了应答，客户端却没收到回调 -> 应答根本没被派发。

def build_sunact_level_sc(rid, level_ids=None):
    """RpcGetSunActLevel_CS -> RpcGetSunActLevel_SC。

    entities/avatarcomponent/avatarsunactcomp.lua:311-321:
      RpcGetSunActLevel_CS() -> serverRpc("RpcGetSunActLevel_CS", {},
                                CallbackHandler(self, "RpcGetSunActLevel_SC"))
      RpcGetSunActLevel_SC(ret, response) -> self.sun_daily_level_ids = response.levelIds
    不回的话 avatarrpccomp.lua:22 的 2 秒定时器会触发 EStartWeakNetTurnCircle，
    然后 DisconnectionPanel -> 重新登录，形成死循环。
    """
    return build_entity_response(rid, resp={"levelIds": list(level_ids or [])})

def build_match_duration_sc(rid):
    """RpcGetMatchDuration -> 应答。

    entities/avatarcomponent/avatarmatch.lua:73-79:
      self:serverRpc("RpcGetMatchDuration", {}, function(retstatus, response)
        if retstatus.status then
          self.predictMatchTime = response.data or {}
        end
      end)
    没有匹配时间数据就回空表，客户端会走 response.data or {}。
    """
    return build_entity_response(rid, resp={"data": {}})

def build_team_info_sc(rid):
    """QueryMyTeamInfo_CS -> 应答（当前不在任何队伍里）。

    entities/avatarcomponent/avatarteamcomp.lua:688-707:
      function AvatarTeamComp:QueryMyTeamInfo_SC(cb, retstatus, response)
        local status = self:CheckTeamStatus(retstatus)
        if status then ... UpdateTeamInfo(response.TeamId, response.TeamInfo) ...
        else self:ResetTeamInfo() end
        if cb then cb(isInTeam) end
      end
    CheckTeamStatus -> retstatus.status 为假时走 ResetTeamInfo()，
    TeamErrorResult(nil, nil) 会因为 errcode=-1 查不到 TeamErrorOrient 而安静返回。
    所以 { status = false } 是"该玩家没有队伍"的合法应答。
    """
    return build_entity_response(rid, ret={"status": False, "errmsg": ""})

def build_entity_message(entity_id="", method_name=None, index=0,
                         params=b"\x80", session_id=None, rid=0):
    """构造 server -> client 的 entityMessage。

    对应 GateClientRpcHandler:entityMessage(entityId, methodName, index, parameters)，
    四个参数都要给：
      1 = entityId   （string）
      2 = methodName （string，客户端按名字找处理方法）
      3 = index      （uint32，genRpcIndex(methodName)）
      4 = parameters （bytes，msgpack）
    """
    body = b""
    if entity_id:
        body += pb_bytes_field(1, entity_id.encode("utf-8"))
    if method_name:
        body += pb_bytes_field(2, method_name.encode("utf-8"))
    if index:
        body += pb_varint_field(3, index)
    if params:
        body += pb_bytes_field(4, params)
    # 字段 5 = rid。客户端 serverRpc 发的 rid 必须原样带回，
    # 否则 core/net/responsemanager 弹不出回调，2 秒后必触发弱网转圈
    # （avatarcomponent/avatarrpccomp.lua:22 turnCircleTime=2）。
    if rid:
        body += pb_varint_field(5, int(rid))
    return frame(2, body)

def parse_entity_frame(body):
    """从客户端 type=4 帧里取出 field 3（index）、field 4（msgpack 参数）、field 5（rid）。

    rid != 0 表示客户端用的是 serverRpc（带回调），服务端必须回一个
    带同样 rid 的应答，否则 2 秒后客户端会弹弱网转圈。
    rid == 0 表示 serverMsg / CallServer（单向），不用回。
    """
    try:
        fields = pb_parse(body)
    except Exception:
        return None, None, 0
    idx = None
    if 3 in fields and isinstance(fields[3][0], int):
        idx = fields[3][0]
    params = fields.get(4, [b""])[0]
    rid = 0
    if 5 in fields and isinstance(fields[5][0], int):
        rid = fields[5][0]
    return idx, params, rid
