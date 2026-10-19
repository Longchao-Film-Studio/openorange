#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.ms —— 微服务（GameLoadService）最小实现

协议已由实测抓包完全解出（见 docs/协议/协议总纲.md 第八部分）：

帧:  [u32 小端 载荷长度][u16 小端 类型=3][protobuf]
请求 protobuf:
    1 (varint) seqId
    2 (len)    服务名    如 "ClientGate" / "GameLoadService"
    3 (len)    方法名    如 "syncMetadata" / "query"
    4 (len)    参数      msgpack 编码的二进制块
    5 (len)    保留（实测为空）
    8 (len)    选项      msgpack 编码的二进制块

本模块提供:
    * 极简 protobuf 解析/编码
    * 极简 msgpack 编码（只需 map/str/int）
    * 应答候选轮换，用于确定应答格式（唯一剩余未知项）

成功判据（无歧义）：客户端收到正确应答后，会去连 ServerList 里 clusters.addrs
指定的网关端口。因此我们让网关监听 33101，ms 监听 33100 ——
**只要 33101 上出现连接，就说明 ms 应答格式对了。**
"""
import struct


# ---------------------------------------------------------------- protobuf
def pb_read_varint(data, i):
    shift = 0
    val = 0
    while True:
        if i >= len(data):
            raise ValueError("varint truncated")
        b = data[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def pb_parse(data):
    """返回 {field_no: [value, ...]}；varint 为 int，len-delimited 为 bytes。"""
    out = {}
    i = 0
    while i < len(data):
        tag = data[i]
        i += 1
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            v, i = pb_read_varint(data, i)
            out.setdefault(fn, []).append(v)
        elif wt == 2:
            ln, i = pb_read_varint(data, i)
            out.setdefault(fn, []).append(data[i:i + ln])
            i += ln
        elif wt == 5:
            out.setdefault(fn, []).append(data[i:i + 4])
            i += 4
        elif wt == 1:
            out.setdefault(fn, []).append(data[i:i + 8])
            i += 8
        else:
            raise ValueError("unsupported wiretype %d at field %d" % (wt, fn))
    return out


def pb_varint_field(fn, v):
    out = bytearray([(fn << 3) | 0])
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_bytes_field(fn, b):
    out = bytearray([(fn << 3) | 2])
    ln = len(b)
    while True:
        x = ln & 0x7F
        ln >>= 7
        if ln:
            out.append(x | 0x80)
        else:
            out.append(x)
            break
    return bytes(out) + b


# ---------------------------------------------------------------- msgpack
def mp_str(s):
    b = s.encode("utf-8")
    n = len(b)
    if n < 32:
        return bytes([0xA0 | n]) + b
    if n < 256:
        return b"\xd9" + bytes([n]) + b
    return b"\xda" + struct.pack(">H", n) + b


def mp_int(v):
    if v < 0:
        return b"\xff" + struct.pack(">q", v)
    if v < 128:
        return bytes([v])
    if v < 256:
        return b"\xcc" + bytes([v])
    if v < 65536:
        return b"\xcd" + struct.pack(">H", v)
    return b"\xce" + struct.pack(">I", v)


def mp_bool(v):
    return b"\xc3" if v else b"\xc2"


def mp_any(v):
    if isinstance(v, bool):
        return mp_bool(v)
    if isinstance(v, int):
        return mp_int(v)
    if isinstance(v, str):
        return mp_str(v)
    if isinstance(v, list):
        return mp_arr(v)
    if isinstance(v, dict):
        return mp_map(v)
    raise TypeError("unsupported msgpack value: %r" % (v,))


def mp_map(d):
    out = bytearray([0x80 | len(d)])
    for k, v in d.items():
        out += mp_str(k)
        out += mp_any(v)
    return bytes(out)


def mp_arr(items):
    out = bytearray([0x90 | len(items)])
    for v in items:
        out += mp_any(v)
    return bytes(out)


# ---------------------------------------------------------------- 服务器列表
# ms 服务端口（clientGate 字段的含义）
#
# ★ 2026-10-19 修正：这两个端口以前被填成同一个值，导致 ms 通道连到了网关端口，
#   客户端表现为 "dispatchRpc ... rpcHandler is invalid" + 3.9 秒重连循环。
#
# 依据 manager/server.lua：
#   Server:ctor(hostnum, name, status, open_time, rid, ms_gate, clusters)
#     self.ms_gate = ms_gate                       ← 来自 server.clientGate
#   Cluster:ctor(cluster_id, addrs, status)
#     self.gates = parseGate(addrs)                ← addrs 是网关地址
#
# 再依据 manager/serverlistmanager.lua：
#   self.ms_gate   = body.clientGate
#   local ms_gate  = server.clientGate
#   if not ms_gate and region then ms_gate = region.ms_gate end
#
# 所以：
#   clientGate  → ms 服务端口（MS_PORT，33100）
#   clusters.addrs → 网关端口（GATE_PORT，33101）
MS_ADVERTISE_PORT = 33100


def build_server_list_json(gate_port=33101, ms_port=None):
    """返回给 ParseFromLoadService 的 JSON 字符串。

    loadcomp 里 response.ServerList 会被 ParseFromLoadService 直接 json.decode，
    所以它必须是一个 JSON **字符串**。

    clientGate 填 ms 端口，clusters[].addrs 填网关端口 —— 两者不能混。
    """
    if ms_port is None:
        ms_port = MS_ADVERTISE_PORT
    return (
        '{"clientGate":"127.0.0.1:%d",'
        '"regions":[{"rid":10,"name":"OpenOrange","clientGate":"127.0.0.1:%d"}],'
        '"servers":[{"hostnum":2000,"name":"OpenOrange","status":1,"openTime":0,"rid":10,'
        '"clientGate":"127.0.0.1:%d",'
        '"clusters":[{"clusterId":20000,"status":1,"addrs":["127.0.0.1:%d"]}]}]}'
        % (ms_port, ms_port, ms_port, gate_port)
    )


# ---------------------------------------------------------------- 应答构造
def build_response_params(gate_port=33101, ret_err=0, ret_status=True):
    """构造 onMicroResponse 的第二个参数 parameters（msgpack 二进制）。

    依据 msgateclientrpchandler.lua L35-49：
        response = codec:decode(parameters)     -- msgpack
        ret      = response.Ret                 -- 必须是 [status, errmsg]
        retStatus = {status = ret[1], errmsg = ret[2], serviceId = serviceId}
        response.Ret = nil                      -- 剩下的就是 response
    """
    return mp_map({
        "Ret": [ret_status, ret_err],
        "ServerList": build_server_list_json(gate_port),
        "ClusterId": 20000,
    })


def build_candidates(rid, gate_port=33101, typ=None):
    """应答候选。
    若给定 typ，则只返回该类型号的一个候选（用于全量爆破）。
    否则返回若干"高可能性"候选。"""
    params = build_response_params(gate_port)
    if typ is not None:
        return [("t%d {1rid,2params}" % typ, typ,
                 pb_varint_field(1, rid) + pb_bytes_field(2, params))]
    cands = []

    # 类型号 4（最可能：请求 3 / 应答 4）
    cands.append(("t4 {1rid,2params}", 4,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)))
    cands.append(("t4 {1rid,2params,3svc}", 4,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params) + pb_bytes_field(3, b"")))
    # 类型号 3
    cands.append(("t3 {1rid,2params}", 3,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)))
    cands.append(("t3 {1rid,2params,3svc}", 3,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params) + pb_bytes_field(3, b"")))
    # 类型号 5 / 2 / 1
    cands.append(("t5 {1rid,2params}", 5,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)))
    cands.append(("t2 {1rid,2params}", 2,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)))
    cands.append(("t1 {1rid,2params}", 1,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)))
    # 带 serviceId 的非空值（"GameLoadService"）
    cands.append(("t4 {1rid,2params,3svcStr}", 4,
                  pb_varint_field(1, rid) + pb_bytes_field(2, params)
                  + pb_bytes_field(3, b"GameLoadService")))
    return cands


def build_by_type(typ, rid, gate_port=33101, ret_status=True, ret_err=0):
    """按类型号构造单个应答。"""
    params = build_response_params(gate_port, ret_err=ret_err, ret_status=ret_status)
    return pb_varint_field(1, rid) + pb_bytes_field(2, params)


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

CONNECT_RESP_TYPE = 1


def build_connect_server_resp(resp_type=1, session_id="OO00000001", content_type=0):
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


def frame_connect_resp(resp_type=1, session_id="OO00000001", content_type=0):
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


def build_load_service_params(gate_port=33101, ret_err=0, ret_status=True):
    """GameLoadService.query 的应答参数（msgpack）。"""
    return mp_map({
        "Ret": [ret_status, ret_err],
        "ServerList": build_server_list_json(gate_port),
        "ClusterId": 20000,
    })

# ============================================================================
# 网关层：bindAccount（服务端主动下发，触发客户端绑定账号实体）
# ============================================================================
#
# 流程（net/networkmanager.lua + core/client/nethandler.lua L109-132）：
#   1. 客户端 connectServer(reqType=0)  ->  服务端 connectResponse(Connected)
#   2. 服务端主动发 bindAccount(auth, actMbBin, clusterId, onlineInfo)
#   3. 客户端 handleBindAccount -> bindAccountHandler -> NetHandler:BindAccount
#        若 self.clusterId == clusterId：新建连接，reqType=4 (BIND_ACCOUNT) 重连
#   4. 服务端在新连接上再回 connectResponse(Connected)
#   5. 账号实体绑定完成
#
# BindAccountInfo（来自 client_gate.proto 描述符 0x61F1DC）：
#   1 actMailbox (EntityMailbox)
#   2 auth       (string)
#   3 clusterId  (uint32)
#   4 onlineInfo (bytes)
#   5 sessionId  (string)
#
# 消息类型号 6（IGateClientService 第 6 项 bindAccount）。

BIND_ACCOUNT_TYPE = 6


def build_entity_mailbox(entity_id="account_2000", server_ip="127.0.0.1",
                         server_port=33101, cluster_id=20000):
    """EntityMailbox { 1:entityId  2:serverIp  3:serverPort  4:clusterId }"""
    body = b""
    if entity_id:
        body += pb_bytes_field(1, entity_id.encode("utf-8"))
    if server_ip:
        body += pb_bytes_field(2, server_ip.encode("utf-8"))
    body += pb_varint_field(3, server_port)
    body += pb_varint_field(4, cluster_id)
    return body


def build_bind_account(auth="", cluster_id=20000, online_info=b"", session_id="OO00000001",
                       act_mailbox=None):
    """构造 BindAccountInfo 的 protobuf 体。

    实测：actMailbox 给空值会让客户端立即断连重连（连接风暴），
    所以这里填一个真实的 EntityMailbox。
    """
    if act_mailbox is None:
        act_mailbox = build_entity_mailbox(cluster_id=cluster_id)
    body = pb_bytes_field(1, act_mailbox)
    if auth:
        body += pb_bytes_field(2, auth.encode("utf-8"))
    body += pb_varint_field(3, cluster_id)
    body += pb_bytes_field(4, online_info)
    if session_id:
        body += pb_bytes_field(5, session_id.encode("utf-8"))
    return body


def frame_bind_account(auth="", cluster_id=20000, online_info=b"", session_id="OO00000001",
                       act_mailbox=None):
    return frame(BIND_ACCOUNT_TYPE,
                 build_bind_account(auth, cluster_id, online_info, session_id, act_mailbox))

# ============================================================================
# 网关层：bindAvatar / bindSoul
# ============================================================================
#
# BindAvatarInfo（描述符 0x61F16E）:
#   1 avtMailbox (EntityMailbox)  2 auth (string)  3 clusterId (uint32)
#   4 onlineInfo (bytes)          5 sessionId (string)
#   类型号 7
#
# BindSoulInfo（描述符 0x61F0F4）:
#   1 soulMailbox (EntityMailbox) 2 auth (string)  3 gates (repeated ServerInfo)
#   4 sessionId (string)
#   类型号 8
#
# ServerInfo（描述符 0x61F00A）: 1 port (uint32)  2 name (string)  3 clusterId (uint32)

BIND_AVATAR_TYPE = 7
BIND_SOUL_TYPE = 8


def build_server_info(port=33101, name="OpenOrange", cluster_id=20000):
    body = pb_varint_field(1, port)
    if name:
        body += pb_bytes_field(2, name.encode("utf-8"))
    body += pb_varint_field(3, cluster_id)
    return body


def build_bind_avatar(auth="", cluster_id=20000, online_info=b"", session_id="OO00000001",
                      avt_mailbox=None):
    if avt_mailbox is None:
        avt_mailbox = build_entity_mailbox("avatar_2000", "127.0.0.1", 33101, cluster_id)
    body = pb_bytes_field(1, avt_mailbox)
    if auth:
        body += pb_bytes_field(2, auth.encode("utf-8"))
    body += pb_varint_field(3, cluster_id)
    body += pb_bytes_field(4, online_info)
    if session_id:
        body += pb_bytes_field(5, session_id.encode("utf-8"))
    return body


def frame_bind_avatar(auth="", cluster_id=20000, online_info=b"", session_id="OO00000001",
                      avt_mailbox=None):
    return frame(BIND_AVATAR_TYPE,
                 build_bind_avatar(auth, cluster_id, online_info, session_id, avt_mailbox))


def build_bind_soul(auth="", session_id="OO00000001", soul_mailbox=None,
                    gates=None, cluster_id=20000):
    if soul_mailbox is None:
        soul_mailbox = build_entity_mailbox("soul_2000", "127.0.0.1", 33101, cluster_id)
    if gates is None:
        gates = [build_server_info()]
    body = pb_bytes_field(1, soul_mailbox)
    if auth:
        body += pb_bytes_field(2, auth.encode("utf-8"))
    for g in gates:
        body += pb_bytes_field(3, g)
    if session_id:
        body += pb_bytes_field(4, session_id.encode("utf-8"))
    return body


def frame_bind_soul(auth="", session_id="OO00000001", soul_mailbox=None,
                    gates=None, cluster_id=20000):
    return frame(BIND_SOUL_TYPE,
                 build_bind_soul(auth, session_id, soul_mailbox, gates, cluster_id))
