#!/usr/bin/env python3
"""帧封装与拆帧（P0-E3 从 ms.py 拆出）。

帧格式：[u32 小端 载荷长度][u16 小端 类型][protobuf]

★ 为什么必须切帧（split_frames 注释里有完整记录）：实测客户端会把两条实体
  消息粘在同一个 TCP 段里发（例如 77 字节 = StartGame_CS 61B + 心跳 16B）。
  旧代码把整块当一帧，protobuf 读到下一帧帧头直接失败，第二条被丢掉 ——
  丢的正好是心跳 index=0，客户端 _recvHeartbeatIndex 就永远停在 0。
"""
import struct

from .defaults import _DEF_UID
from .protocol import pb_bytes_field, pb_varint_field


def frame(msg_type, body):
    payload = struct.pack("<H", msg_type) + body
    return struct.pack("<I", len(payload)) + payload

# ============================================================================
# 网关层：ConnectServerResp（已从二进制里的 .proto 描述符完整解出）
# ============================================================================
#
# 来源：xlua.dll_unpacked.bin 里 client_gate.proto 的 FileDescriptorProto
#   0x61F350  ConnectServerResp
#   0x61F365  type          <- 字段 1，枚举 ConnectServerResp.RespType
#   0x61F39E  sessionId     <- 字段 2，string
#   0x61F3B2  RespType
#   0x61F3BE    Busy              = 0
#   0x61F3C8    Connected         = 1
#   0x61F3D7    ConnectRefused    = 2
#   0x61F3EB    Reconnected       = 3
#   0x61F3FC    ReconnectRefused  = 4
#   0x61F412    Forbidden         = 5
#   0x61F421    MaxConnections    = 6
#   0x61F435    RetryConnect      = 7
#
# 消息类型号：来自注册表（rva 0x0348E0 处的 {跳板, 类型号} 表）
#   IGateClientService 的方法顺序：
#     1 connectResponse   2 entityMessage     3 channelMessage
#     4 createChannelEntity  5 disconnectClient  6 bindAccount
#     7 bindAvatar        8 bindSoul          9 unbindSoul
#    10 responseEncryptToken  11 confirmEncryptKeyAck  12 recvHeartbeat
#    13 propertySync2Client   14 propertyStrategySync2Client
#   => connectResponse 的类型号 = 1

def split_frames(buf):
    """按 [u32 LE payloadLen][u16 LE msgType][payload] 切帧。

    返回 (帧列表, 剩余不足一帧的尾巴)。

    ★ 为什么必须切帧：实测客户端会把两条实体消息粘在同一个 TCP 段里发。
      例如 77 字节 = Cmd_Avatar_StartGame_CS(61 B) + 心跳 index=0(16 B)。
      旧代码把整块当一帧，protobuf 读到下一帧帧头 0C（field1 wiretype4）
      直接失败，第二条被丢掉——丢的正好是心跳 index=0，
      客户端 _recvHeartbeatIndex 就永远停在 0。
    """
    frames = []
    off = 0
    n = len(buf)
    while n - off >= 6:
        plen = struct.unpack_from("<I", buf, off)[0]
        total = 4 + plen
        if total < 6 or plen > MAX_FRAME_LEN:
            # ★ 非法帧头（plen 为 0/1 或超过上限）：丢掉 1 字节重同步。
            #   以前这里是 break，off 永远不前进 -> buf 只增不减、后续合法帧
            #   永远处理不到，单连接彻底卡死（畸形客户端/端口扫描即可触发）。
            off += 1
            continue
        if n - off < total:
            break
        frames.append(bytes(buf[off:off + total]))
        off += total
    return frames, bytes(buf[off:])

# 单帧载荷上限（1 MiB）：超过就当成畸形帧头，丢字节重同步。
# 和 transport/ms_service.py 里读 ms 帧时的上限保持一致。
MAX_FRAME_LEN = 1 << 20

CONNECT_RESP_TYPE = 1

def build_connect_server_resp(resp_type=1, session_id=None, content_type=0):
    session_id = session_id or _DEF_UID
    """构造 ConnectServerResp 的 protobuf 体。

    ★ 关键：content_type（字段 5）必须**显式写出**，即使值为默认的 0。
    实测对比（同一套 ms 应答，只改网关应答）：
        无应答                                  -> 连接 3981 ms 后关闭
        {1:1}                                   -> 4012 ms
        {1:1, 2:"OO00000001"}                   -> 4012 ms
        {1:3, 2:"OO00000001"}                   -> 4114 ms
        {1:1, 2:长串}                            -> 3865 ms
        {1:1, 2:"OO00000001", 5:0}              -> **60019 ms，活过 4 秒** ✅
    原因：phonest 的 protobuf 生成代码用 proto2 的 has-field 语义，
          客户端会检查 has_content_type()，缺字段即判定应答无效。
    """
    body = pb_varint_field(1, resp_type)
    if session_id:
        body += pb_bytes_field(2, session_id.encode("utf-8"))
    body += pb_varint_field(5, content_type)
    return body

def frame_connect_resp(resp_type=1, session_id=None, content_type=0):
    """构造完整的 ConnectServerResp 帧。"""
    return frame(CONNECT_RESP_TYPE,
                 build_connect_server_resp(resp_type, session_id, content_type))

# ============================================================================
# 微服务层：MsResponse（已从 ms_common.proto 描述符完整解出）
# ============================================================================
#
# 0x5F6340  MsResponse
#   0x5F634E  rid           <- 字段 1
#   0x5F635B  params        <- 字段 2（bytes，内容是 msgpack）
#   0x5F636B  context       <- 字段 3（RpcContext）
#   0x5F639C  service_id    <- 字段 4（string）
#   0x5F63B0  content_type  <- 字段 5（枚举 ContentType: MSGPACK=0 JSON=1 XML=2 PROTOBUF=3 CUSTOM=4）
#
# 类型号来自枚举顺序（0x5F64EE 处的 EnumG2C）：
#   ResponseEncryptToken=1, ConfirmEncryptKeyAck=2, MicroResponse=3, MicroPush=4, Heartbeat=5
# 客户端发来的 MicroRequest 同样是 3（EnumC2G 里它是第 3 个）。
#
# 客户端侧解析（msgateclientrpchandler.lua L35-49）：
#   response = msgpack.unpack(params)
#   ret      = response.Ret          -- 必须是 [status, errmsg]
#   response.Ret = nil               -- 剩下的就是 response
#
# loadcomp.lua 需要 response.ServerList（JSON 字符串）与 response.ClusterId。

MS_RESP_TYPE = 3

def build_ms_response(rid, params_bytes, service_id=""):
    """构造 MsResponse 的 protobuf 体。"""
    body = pb_varint_field(1, rid)
    body += pb_bytes_field(2, params_bytes)
    if service_id:
        body += pb_bytes_field(4, service_id.encode("utf-8"))
    return body

def frame_ms_response(rid, params_bytes, service_id=""):
    """构造完整的 MsResponse 帧。"""
    return frame(MS_RESP_TYPE, build_ms_response(rid, params_bytes, service_id))

# ============================================================================
# 微服务层：MsPush（帧类型 4）—— 服务端主动推给客户端
# ============================================================================
#
# 字段号来自 ms_common.proto 的 FileDescriptorProto，**逐字节解出来的**：
#   F:\ghidra\out\xlua_unpacked.dll @0x5f6200（tag 0a 0f + "ms_common.proto"，共 1124 字节）
#
#   message MsPush {
#       2 = service   required string
#       3 = method    required string
#       4 = params    optional bytes
#       5 = msg_uuid  optional string
#       6 = ep_ids    repeated string
#       7 = org_type  optional string
#   }
#
# ★★ 不是 1..6。1 号位在这个 .proto 里是空的（同 MsRequest 跳过了 6 号位，
#    都是"手写 .proto 删过字段"的痕迹）。2026-10-31 之前这里按"声明顺序从 1 开始"
#    写了 1..6，导致发出去的推送被客户端解析成 service=newMailNotice /
#    method=<二进制>，**静默失效**。教训：descriptor 里 number 是显式存的，不许推。
#
# 帧类型 4 来自 EnumG2C（同文件）：
#   ResponseEncryptToken=1, ConfirmEncryptKeyAck=2, MicroResponse=3, MicroPush=4, Heartbeat=5
#
# 客户端侧（core/microservice/msservicemanager.lua:138-180）：
#   onPush(orgType, epIds, msgId, serviceName, methodName, parameters)
#     method = serviceName == "" and methodName or (serviceName .. "_" .. methodName)
#     func   = target[method]      -- target = 该 ep 注册的推送端点（= ClientAvatar）
#     SafeCallback(func, target, unpack(parameters or {}))
#
# 所以 parameters 必须是**数组**，且第一个元素就是方法要的实参。
# 例：邮件推送 methodName="newMailNotice"、parameters={mailsInfo}。

MS_PUSH_TYPE = 4

#: MsPush 的字段号（**来自描述符，不许改**，理由见上面的注释）
MS_PUSH_F_SERVICE = 2
MS_PUSH_F_METHOD = 3
MS_PUSH_F_PARAMS = 4
MS_PUSH_F_MSG_UUID = 5
MS_PUSH_F_EP_IDS = 6
MS_PUSH_F_ORG_TYPE = 7


def build_ms_push(org_type, ep_ids, service, method, params_bytes, msg_uuid=""):
    """构造 MsPush 的 protobuf 体。

    org_type: "ENTITY"（按 ep_ids 定向）/ "CLUSTER" / "WORLD"（发给所有端点）
    ep_ids:   目标端点 id 列表（客户端注册时用的 id，这里就是 uid）
    msg_uuid: 可靠消息 id；空串 = 不可靠（客户端不 ack）
    """
    body = pb_bytes_field(MS_PUSH_F_SERVICE, service.encode("utf-8"))
    body += pb_bytes_field(MS_PUSH_F_METHOD, method.encode("utf-8"))
    body += pb_bytes_field(MS_PUSH_F_PARAMS, params_bytes)
    if msg_uuid:
        body += pb_bytes_field(MS_PUSH_F_MSG_UUID, msg_uuid.encode("utf-8"))
    for ep in ep_ids:
        body += pb_bytes_field(MS_PUSH_F_EP_IDS, str(ep).encode("utf-8"))
    body += pb_bytes_field(MS_PUSH_F_ORG_TYPE, org_type.encode("utf-8"))
    return body


def frame_ms_push(org_type, ep_ids, service, method, params_bytes, msg_uuid=""):
    """构造完整的 MsPush 帧（帧类型 4）。"""
    return frame(MS_PUSH_TYPE, build_ms_push(org_type, ep_ids, service, method,
                                             params_bytes, msg_uuid))

