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
import json
import os
import struct

# pb / msgpack 编解码已拆到 protocol.py；userdata 应答在 userdata.py。
# 这里 re-export，保持 msmod.pb_parse / msmod.mp_arr 等既有调用点不变。
from .protocol import (  # noqa: F401  (re-export)
    mp_any,
    mp_arr,
    mp_bool,
    mp_decode,
    mp_float,
    mp_int,
    mp_map,
    mp_str,
    pb_bytes_field,
    pb_parse,
    pb_read_varint,
    pb_varint_field,
)
from .userdata import build_userdata_batch_params  # noqa: F401  (re-export)

# P0-E3：ms.py 已按职责拆成 protocol / frames / entity / property / bind / userdata，
# 这里统一 re-export，保持 msmod.xxx 的全部既有调用点不变。
from .defaults import (  # noqa: F401  (re-export)
    _DEF_ACCOUNT_EID,
    _DEF_AVATAR_EID,
    _DEF_UID,
)
from .frames import (  # noqa: F401  (re-export)
    CONNECT_RESP_TYPE,
    MS_RESP_TYPE,
    build_connect_server_resp,
    build_ms_response,
    frame,
    frame_connect_resp,
    frame_ms_response,
    split_frames,
)
from .protocol import _conv_int_keys  # noqa: F401  (re-export)
from .initdict import (  # noqa: F401  (re-export)
    AVATAR_INIT_DICT,
    CLIENT_AVATAR_CLASS,
    DEFAULT_INIT_DICT,
    MSGPACK_EMPTY_MAP,
    _LEGACY_INIT_DICT,
    _live_init_dict,
    _login_tick,
    build_channel_entity,
)
from .entity import (  # noqa: F401  (re-export)
    ENTITY_RESPONSE_FRAME_TYPE,
    build_entity_message,
    build_entity_response,
    build_match_duration_sc,
    build_sunact_level_sc,
    build_team_info_sc,
    parse_entity_frame,
    parse_entity_response,
)
from .property import (  # noqa: F401  (re-export)
    OP_ADD,
    OP_CHANGE,
    OP_DEL,
    OP_INSERT,
    OP_REMOVE,
    PROPERTY_SYNC_TYPE,
    RPC_INDEX_SET_SIGNATURE_CS,
    RPC_INDEX_UPGRADE_CHARACTER_CS,
    RPC_INDEX_USE_ITEM_CS,
    build_property_sync,
)
from .bind import (  # noqa: F401  (re-export)
    BIND_ACCOUNT_TYPE,
    BIND_AVATAR_TYPE,
    BIND_SOUL_TYPE,
    build_bind_account,
    build_bind_avatar,
    build_bind_soul,
    build_entity_mailbox,
    build_server_info,
    frame_bind_account,
    frame_bind_avatar,
    frame_bind_soul,
)

try:
    from .. import config as ooconf
except Exception:
    import config as ooconf  # type: ignore

# ★ 默认值统一从 config 取，不要在这里写死端口/uid/实体 id。

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

# ---------------------------------------------------------------- 实体
# ---------------------------------------------------------------- 实体消息
#
# server -> client 的实体消息（GateClientRpcHandler:entityMessage 的入参）：
#   entityMessage(entityId, methodName, index, parameters)
#
# 帧类型 2（IGateClientService 第 2 个方法）。
#
# 字段布局按 client->server 的 sendEntityMessage 镜像推断（实测那边是
#   1=entityId(空省略) 3=genRpcIndex(name) 4=msgpack(params) 5=rid(0省略)）：
#   1=entityId  3=index  4=msgpack(params)
#
# 目前只是试探：收到客户端的 type=4 帧就 echo 它的 field 3 回去，参数给空 map。
# ---------------------------------------------------------------- 时间同步
#
# 客户端 AvatarTimeComp:syncFromServer() 会发：
#     self:serverMsg("RpcSyncServerTime", { os.time() })
# 也就是 genRpcIndex("RpcSyncServerTime") = 15228564，参数是 msgpack [客户端时间]。
#
# 服务端要回 RpcOnSyncServerTime，参数是 { ct = 客户端时间, st = 服务器时间 }：
#     function AvatarTimeComp:RpcOnSyncServerTime(data)
#       local rtt = os.time() - data.ct
#       G.timemgr:SyncFromServer(data.st)
#     end
RPC_SYNC_SERVER_TIME = 15228564        # 客户端 -> 服务端
RPC_ON_SYNC_SERVER_TIME = 1381181593   # 服务端 -> 客户端
RPC_NETWORK_CHECK = 1259880996         # networkCheckAfterChange_CS（客户端心跳）
RPC_NETWORK_CHECK_SC = 151106036       # networkCheckAfterChange_SC（服务端必须回）
RPC_INDEX_NOT_FOUND = 1501621037       # __rpc_index_not_found__

# 2026-10-19 用 phonestcore.genRpcIndex 对 73470 条 Lua 字符串字面量做全量哈希反查
# （tools 里 work/oo_analysis/hash_all.py + rpc_hashes.tsv），把登录后客户端发出的
# 两个未知 index 定死了：
#   2074889135 -> RpcPullHotfix        （entities/commoncomponent/basecomp.lua:8 CallServer）
#   1033592883 -> RpcGetSunActLevel_CS （entities/avatarcomponent/avatarsunactcomp.lua:12）
RPC_PULL_HOTFIX = 2074889135
RPC_GET_SUNACT_LEVEL_CS = 1033592883
RPC_GET_SUNACT_LEVEL_SC = 405867711
# 136701728 也是同一张表查出来的：clientavatar.lua:196 序章 StartGame 用的那个。
RPC_CMD_AVATAR_STARTGAME_CS = 136701728
# 同一张表查出来的：
#   1795313897 -> RpcGetMatchDuration（entities/avatarcomponent/avatarmatch.lua:74
#                 serverRpc 带内联回调，不回也是 2 秒弱网）
RPC_GET_MATCH_DURATION = 1795313897
# 应答名游戏里没有对应方法（_CS 用的是内联闭包），按 _CS/_SC 约定补一个，
# 并在 launcher/lua/oo_bootstrap.lua 的 SERVER_RPC_SC_NAMES 里注册，
# 否则原生 RpcIndex::getRpcName 查不到名字，应答派发不下去。
RPC_GET_MATCH_DURATION_SC = 520324610
#   1674960337 -> QueryMyTeamInfo_CS（entities/avatarcomponent/avatarteamcomp.lua:684
#                 HallStage -> HallLogic:active -> CheckMyTeamInfo_CS -> QueryMyTeamInfo_CS）
RPC_QUERY_MY_TEAM_INFO_CS = 1674960337

def build_time_sync(client_time, server_time):
    """构造 RpcOnSyncServerTime 应答。

    ★ parameters 是**参数数组**，不是单个 map。
    客户端 RpcCall 会把 parameters 当参数列表展开：
        RpcMethod(RpcOnSyncServerTime) need args num 1, but got 0
    所以 RpcOnSyncServerTime(data) 要发 [{ ct = ..., st = ... }]。
    """
    payload = mp_arr([{"ct": int(client_time), "st": int(server_time)}])
    return build_entity_message(method_name="RpcOnSyncServerTime",
                                index=RPC_ON_SYNC_SERVER_TIME,
                                params=payload)

def build_net_check_ack(index):
    """构造 networkCheckAfterChange_SC(index) 心跳应答。

    依据 entities/avatarcomponent/avatarnetcomp.lua：
      _heartbeatTimerCB() 每秒发一次 networkCheckAfterChange_CS({_sendHeartbeatIndex})
      networkCheckAfterChange_SC(index) -> self._recvHeartbeatIndex = index
    服务端不回，_recvHeartbeatIndex 永远是 0，
    差值超过 15 后客户端就会 closeClient() + tryReConnect()。
    """
    payload = mp_arr([int(index)])
    return build_entity_message(method_name="networkCheckAfterChange_SC",
                                index=RPC_NETWORK_CHECK_SC,
                                params=payload)

# ---------------------------------------------------------------------------
# UserDataService.batchGetAttribute（1-3 玩家数据的主通道）
# ---------------------------------------------------------------------------
# 客户端 entities/avatarcomponent/avataruserdatacomp.lua:118-162：
#   requests = { { tostring(uid), {属性名...} }, ... }
#   self:callService("UserDataService", "batchGetAttribute", {list}, cb)
# 应答（同文件 164-212 的 AvatarUserDataComp_GetUsersData_cb）：
#   response.Results = { { Uid = <字符串>, AttributesMap = { 属性名 = 值 } }, ... }
#   if uid == Uid then ... setOneAttributeData(key, attrs[key])
# 注意 Uid 是严格比较，请求里是 tostring(uid)，所以必须回字符串。
_SIMPLE_TYPES = (str, int, float, bool)

# ---------------------------------------------------------------------------
# propertySync2Client（s->c 方法 13）
# ---------------------------------------------------------------------------
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
