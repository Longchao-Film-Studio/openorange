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

try:
    from . import config as ooconf
except Exception:
    import config as ooconf  # type: ignore

# ★ 默认值统一从 config 取，不要在这里写死端口/uid/实体 id。
_DEF_UID = ooconf.FALLBACK_UID
_DEF_AVATAR_EID = ooconf.avatar_entity_id(ooconf.FALLBACK_UID)
_DEF_ACCOUNT_EID = ooconf.account_entity_id(ooconf.FALLBACK_UID)


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


def mp_float(v):
    """msgpack float64（0xCB）。"""
    return b"\xcb" + struct.pack(">d", float(v))


def mp_any(v):
    if v is None:
        # msgpack nil（0xC0）。UseItem_CS 的 ext 参数经常是 nil。
        return b"\xc0"
    if isinstance(v, float):
        return mp_float(v)
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
    """msgpack map。

    ★ 超过 15 项必须用 map16(0xDE)，不能再用 0x80|n ——
    0x80|173 = 0xAD 是 fixstr(13) 的标记，客户端会把整张表当字符串解，
    报 "bad argument #1 to 'pairs' (table expected, got string)"。
    """
    n = len(d)
    if n < 16:
        out = bytearray([0x80 | n])
    else:
        out = bytearray(b"\xde" + struct.pack(">H", n))
    for k, v in d.items():
        # 键也要走 mp_any：自定义字典里 __IntTypeKey__=true 的类型（如
        # BooleanDictKeyInt）键必须是 msgpack 整数。见 _conv_int_keys。
        out += mp_str(k) if isinstance(k, str) else mp_any(k)
        out += mp_any(v)
    return bytes(out)


def mp_arr(items):
    """msgpack array。同样：超过 15 项要用 array16(0xDC)。"""
    n = len(items)
    if n < 16:
        out = bytearray([0x90 | n])
    else:
        out = bytearray(b"\xdc" + struct.pack(">H", n))
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


# ---------------------------------------------------------------- 实体
#
# ChannelEntityInfo 的字段 1 是 **string**，不是 uint32。
#
# 依据（实测，2026-10-19）：
#   Lua 侧 GateClientRpcHandler:createChannelEntity(entityType, initDict, entityId)
#   把 entityType 直接传给 EntityFactory.createEntity(name, entityId) —— 收的是类名字符串。
#   把字段 1 按 varint 发（哪怕值正好是 genRpcIndex("ClientAvatar")=1292148699），
#   客户端日志是：
#       [GateClientRpcHandler] createChannelEntity (, avatar_2000) failed
#       [EntityFactory] createEntity failed: can not find entity class
#   第一个参数是**空字符串** —— 说明字段 1 被当成 string 解，varint 解不出来。
#
#   另外：RpcIndex 表确实能反查（registerRpc 之后 getRpcName(1292148699) 返回
#   "ClientAvatar"），但客户端并不用它来还原这个名字，所以补注册实体类名是
#   无害但也不是必需的。真正关键的是字段类型。
#
# 字段布局（推断）：
#   string type = 1;  bytes info = 2;  string entityId = 3;  string sessionId = 4
CLIENT_AVATAR_CLASS = "ClientAvatar"

# msgpack 空 map —— 作为最小 initDict 先试水
MSGPACK_EMPTY_MAP = b"\x80"


# 玩家实体的最小 initDict。
#
# ★ 结构必须是这样（依据 Core/PropertySync/PuppetPropertyComponent.lua:initProperty）：
#
#     local properties = dict.__Properties__      -- 没有这层包裹就直接 return
#     if properties == nil then return end
#     for name, value in pairs(properties) do
#       if type(value) == "table" then
#         local customType = value.__tp__          -- table 类型必须带 __tp__
#         assert(customType ~= nil)
#         value.__tp__ = nil
#         CustomTypeFactory.createReadonly(customType, name, value, self, nil, true)
#
#   而 customDict 的 roinit 又要求：
#     local id = value.__id__
#     value.__id__ = nil
#     assert(id ~= nil)                            -- 必须带 __id__
#
#   所以自定义字典属性的写法是 { __tp__ = "customDict", __id__ = N }。
#
# 之前直接给 {"item_bag": []}，因为没有 __Properties__ 包裹，
# initProperty 第一行就 return 了，属性一个都没设上 ——
# 于是 AvatarBagComp:refreshLocalBagData 里 pairs(self.item_bag) 拿到 nil。
#
# 后续每补一个属性就能多过一关，按客户端报错逐个加。
# item_bag 的类型是 config.lua Custom 段里的 ItemBag：
#   ItemBag = { NameSpace = "CustomTypes.Bag.ItemBag",
#               Properties = {}, __ValueType__ = "ItemInfo", __IntTypeKey__ = false }
# __tp__ 必须填**注册名**（ItemBag），不能填 PropertyCallbacks 里的类型标签（customDict）——
# 填 customDict 会报 "can not find custom class customDict"。
# ★ 完整属性表从 config.lua 的 AvatarProperties 生成（tools: work/oo_analysis/gen_initdict.py）
#
# config.lua 里 AvatarProperties.Properties 是玩家实体属性的权威声明，
# 每条形如 {propType, default, aoiscope, persist}。共 173 条：
#   45 个简单类型（int/string/double/boolean）-> 直接给默认值
#   128 个自定义类型 -> 给 { __tp__ = <类型名>, __id__ = <唯一整数> }
#
# 这份表由 game_server/avatar_init.json 提供，避免手写遗漏。
_AVATAR_INIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar_init.json")


def _conv_int_keys(obj):
    """把 JSON 里 "#<int>" 形式的键转成真正的整数键。

    JSON 的对象键只能是字符串，但客户端自定义字典（CustomDict:roinit）
    会把键原样放进 _properties，不做类型转换；__IntTypeKey__=true 的类型
    必须收到整数键。所以生成器里用 "#1" 这种写法表示整数 1。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("#") and k[1:].lstrip("-").isdigit():
                out[int(k[1:])] = _conv_int_keys(v)
            else:
                out[k] = _conv_int_keys(v)
        return out
    if isinstance(obj, list):
        return [_conv_int_keys(v) for v in obj]
    return obj


def _load_avatar_init():
    """读取 avatar_init.json。

    ★ 必须响亮地失败：之前这个文件被写坏成 0 字节，
      静默降级成空表，导致属性全是 nil，白白排查了很久。
    """
    try:
        with open(_AVATAR_INIT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        raise RuntimeError("avatar_init.json 读取失败: %s" % e)
    props = d.get("__Properties__")
    if not isinstance(props, dict) or len(props) < 10:
        raise RuntimeError(
            "avatar_init.json 内容异常：__Properties__ 只有 %s 项（文件 %s）"
            % (len(props) if isinstance(props, dict) else props, _AVATAR_INIT_PATH))
    return d


AVATAR_INIT_DICT = _load_avatar_init()

# 旧的手写最小表，保留作为参考/兜底
_LEGACY_INIT_DICT = {
    "__Properties__": {
        "item_bag": {"__tp__": "ItemBag", "__id__": 1},
        "task_bag": {"__tp__": "TaskBag", "__id__": 2},
        "cur_activity_info": {"__tp__": "ActivityProp", "__id__": 3},
    },
}

# 每个自定义字典属性都要给 { __tp__ = <注册名>, __id__ = <唯一整数> }。
#
# 逐条补的依据都是客户端异常（Player.log）：
#   AvatarBagComp:refreshLocalBagData    -> pairs(self.item_bag)  nil
#     => item_bag, 类型 ItemBag  (__ValueType__=ItemInfo,  __IntTypeKey__=false)
#   AvatarTaskComp:getAllDoingDailyTaskItem -> self.task_bag.*  nil
#     => task_bag, 类型 TaskBag  (__ValueType__=TaskItem,  __IntTypeKey__=true)
# 实际使用的 initDict：优先用 AvatarProperties 生成的完整表
DEFAULT_INIT_DICT = AVATAR_INIT_DICT if AVATAR_INIT_DICT.get("__Properties__") else _LEGACY_INIT_DICT


def _live_init_dict(uid=None):
    """登录时的 initDict 用服务端可变状态（player_state）。

    这样 Set* 改过的值、以及落盘后重启的存档，都会在下次登录时生效。
    uid 不为空时把 initDict 里的 uid 覆盖成它 —— 多账号时每个账号用自己的 uid
    （现在 uid 直接复用账号名，见 game_server/server.py 的在线账号关联）。
    """
    try:
        from . import player_state as pstate
    except Exception:
        try:
            import player_state as pstate
        except Exception:
            return DEFAULT_INIT_DICT
    try:
        # ★ 服务端状态必须用整数键（和客户端一致）。
        #   avatar_init.json 里是 "#9001" 这种写法，_conv_int_keys 之后才是 9001。
        #   以前只有发包时才转换，导致 player_state 里存的是 "#9001"，
        #   改数据、算同步的 name 时对不上。
        st = pstate.get_state(_conv_int_keys(AVATAR_INIT_DICT.get("__Properties__") or {}),
                              uid=uid)
    except Exception:
        return DEFAULT_INIT_DICT
    # 账号 uid 写进状态本身（多账号时每个账号用自己的 uid）
    if uid:
        st.props["uid"] = uid
    # ★ 脚手架覆盖层：只在「该账号首次建状态」时应用一次，默认关闭。
    #   以前是烤进 avatar_init.json 的，生成后关不掉，见 scaffold.py 头部注释。
    # 只有「新建存档」才应用脚手架（老存档里的进度不能被覆盖）
    if getattr(st, "is_new", False) and not getattr(st, "_scaffold_applied", False):
        try:
            from . import scaffold as scaffold_mod
        except Exception:
            try:
                import scaffold as scaffold_mod
            except Exception:
                scaffold_mod = None
        if scaffold_mod is not None:
            try:
                changed = scaffold_mod.apply(st.props)
                if changed:
                    print("[init] 脚手架已应用(uid=%s): %s"
                          % (uid, "、".join(changed)), flush=True)
            except Exception as e:
                print("[init] 脚手架应用失败: %s" % e, flush=True)
        st._scaffold_applied = True

    # ★ 新号的剧情起点（正式路径，不是脚手架）：
    #   把 StoryTaskData[1000] 设为进行中。街景 NPC 的 preTask 命中
    #   Doing(3) 才会出现，所以这一步决定「街上有没有 NPC」。
    #   脚手架开了「任务全完成」时跳过，避免和假数据打架。
    if getattr(st, "is_new", False):
        try:
            from . import story as story_mod
        except Exception:
            try:
                import story as story_mod
            except Exception:
                story_mod = None
        if story_mod is not None:
            try:
                if not ooconf.SCAFFOLD.all_tasks_done:
                    story_mod.init_new_player(st)
            except Exception as e:
                print("[init] 剧情起点设置失败: %s" % e, flush=True)

    # ★ 体力：新号拉满；每次登录按离线时间补。
    #   以前 tower_reward_keys 默认 0、两个时间戳也 0，
    #   导致「开局不满、也不随时间恢复」。见 game_server/stamina.py。
    try:
        from . import stamina as stamina_mod
    except Exception:
        try:
            import stamina as stamina_mod
        except Exception:
            stamina_mod = None
    if stamina_mod is not None:
        try:
            _lv = int(st.props.get("level") or 1)
            if getattr(st, "is_new", False) and not getattr(st, "_stamina_inited", False):
                stamina_mod.init_new(st.props, _lv)
                st._stamina_inited = True
                print("[init] 体力初始化(uid=%s): %s/%s"
                      % (uid, st.props.get("tower_reward_keys"),
                         stamina_mod.max_stamina(_lv)), flush=True)
            else:
                _gained, _ = stamina_mod.apply_recovery(st.props, _lv)
                if _gained:
                    print("[init] 离线恢复体力(uid=%s): +%s -> %s"
                          % (uid, _gained, st.props.get("tower_reward_keys")), flush=True)
        except Exception as e:
            print("[init] 体力处理失败: %s" % e, flush=True)
    return {"__Properties__": st.props}


def build_channel_entity(entity_type=CLIENT_AVATAR_CLASS,
                         init_dict=None,
                         entity_id=_DEF_AVATAR_EID,
                         session_id=_DEF_UID,
                         uid=None):
    """构造 createChannelEntity（IGateClientService 第 4 个方法）。

    字段 1 按 **string** 发类名（如 "ClientAvatar"）；
    字段 2 是 msgpack 编码的 initDict。
    """
    if not isinstance(entity_type, bytes):
        entity_type = str(entity_type).encode("utf-8")
    if init_dict is None:
        init_dict = _live_init_dict(uid)
    info = mp_map(_conv_int_keys(init_dict)) if init_dict else MSGPACK_EMPTY_MAP
    body = pb_bytes_field(1, entity_type)
    body += pb_bytes_field(2, info)
    body += pb_bytes_field(3, entity_id.encode("utf-8"))
    body += pb_bytes_field(4, session_id.encode("utf-8"))
    return frame(4, body)


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


def mp_decode(b, i=0):
    """最小 msgpack 解码器，够用就行。返回 (值, 新下标)。"""
    if i >= len(b):
        return None, i
    c = b[i]; i += 1
    if c <= 0x7F:
        return c, i
    if c >= 0xE0:
        return c - 0x100, i
    if 0x80 <= c <= 0x8F:                      # fixmap
        n = c & 0x0F
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if 0x90 <= c <= 0x9F:                      # fixarray
        n = c & 0x0F
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if 0xA0 <= c <= 0xBF:                      # fixstr
        n = c & 0x1F
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xCA:                              # float32
        return struct.unpack_from(">f", b, i)[0], i + 4
    if c == 0xCB:                              # float64
        # initDict 里 expireTime / getTime 是 double，编码就是 0xCB。
        # 以前没处理这个分支，会退回 (None, i+1) 导致整条流错位。
        return struct.unpack_from(">d", b, i)[0], i + 8
    if c == 0xCC:
        return b[i], i + 1
    if c == 0xCD:
        return int.from_bytes(b[i:i + 2], "big"), i + 2
    if c == 0xCE:
        return int.from_bytes(b[i:i + 4], "big"), i + 4
    if c == 0xCF:
        return int.from_bytes(b[i:i + 8], "big"), i + 8
    if c == 0xD0:
        return int.from_bytes(b[i:i + 1], "big", signed=True), i + 1
    if c == 0xD1:
        return int.from_bytes(b[i:i + 2], "big", signed=True), i + 2
    if c == 0xD2:
        return int.from_bytes(b[i:i + 4], "big", signed=True), i + 4
    if c == 0xD3:
        return int.from_bytes(b[i:i + 8], "big", signed=True), i + 8
    if c == 0xD9:
        n = b[i]; i += 1
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDA:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDB:                              # str32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDC:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if c == 0xDD:                              # array32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if c == 0xDE:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if c == 0xDF:                              # map32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if c in (0xC0,):
        return None, i
    if c in (0xC2, 0xC3):
        return c == 0xC3, i
    return None, i


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


# server -> client 的应答帧类型。
# ★ 逆向确定：应答不是独立帧类型，而是 entityMessage(type=2) 带 methodName=__rpc_response__。
#   依据：client_gate.proto 的 IGateClientService 只有
#   1=connectResponse 2=entityMessage 3=channelMessage 4=createChannelEntity 5=disconnectClient ...
#   没有 entityMessageResponse；而 xlua.dll 镜像里 GateClientProtobufHandler 的
#   字符串表有 __rpc_response__ 这个魔数。
ENTITY_RESPONSE_FRAME_TYPE = 2
# 探测用：把同一个应答再换几个帧类型发一遍，让客户端自己认领。
# 已确认 1/2/3/4 都被占用（3=channelMessage，实测把 chanMsg 异常从 5 次降到 0），
# 5 也不对（回调仍超时）。所以 6..9 一起试，哪个能让 _SC 回调拿到 status=true 就是它。
# ★ 2026-10-20：探测结论 —— 这些类型客户端都不认，先关掉（留代码备查）。
#   实测：3=channelMessage（发 type=3 时客户端报 chanMsg nil，改成别的类型后归零）；
#   5/6/7/8/9 全试过，客户端 _SC 回调仍然 rpc timeout。
ENTITY_RESPONSE_PROBE_TYPES = ()
# 候选 2（type=2 entityMessage + field5=rid）也试过，不认（客户端报 got empty methodName）。
ENTITY_RESPONSE_TRY_MESSAGE = False
# 5 组 (帧类型, 字段布局) 组合也全不认（实测 5 个 _SC 回调全部 rpc timeout），默认关。
ENTITY_RESPONSE_TRY_CANDIDATES = False



# ---------------------------------------------------------------------------
# 应答帧格式穷举（临时实验，见 ROADMAP 第 24 节）
# ---------------------------------------------------------------------------
# entityMessageResponse(rid, parameters) 的确切帧类型和字段编号未知。
# 已知 s->c: 1=connectResponse 2=entityMessage 3=channelMessage 4=createChannelEntity。
# 设计：客户端一次并发发 5 条不同的 setter RPC，rid 连续；
#       服务端按 rid % 5 选不同的 (帧类型, 字段布局) 组合回，
#       哪个回调拿到 status=true 就说明那组是对的。
def _cand_payload(ret, resp):
    if ret is None:
        ret = {"status": True, "errmsg": ""}
    if resp is None:
        resp = {}
    return mp_arr([ret, resp])



# ---------------------------------------------------------------------------
# 应答帧格式二维穷举（临时实验）
# ---------------------------------------------------------------------------
# 一次测 20 组：combo = rid % 20；type = BASE + combo // 4；layout = combo % 4
# BASE 从 F:/haowancheng/work/oo_analysis/resp_type_base.txt 读，
# 这样换一批帧类型不用改代码。
_RESP_BASE_FILE = r"F:/haowancheng/work/oo_analysis/resp_type_base.txt"


def _resp_type_base():
    try:
        return int(open(_RESP_BASE_FILE, encoding="utf-8").read().strip())
    except Exception:
        return 5


RESP_PROBE_TYPE_BASE = _resp_type_base()


def build_entity_response_combo(rid, combo):
    """combo 0..19 -> (帧类型, 字段布局)。"""
    payload = _cand_payload(None, None)
    r = int(rid)
    ftype = RESP_PROBE_TYPE_BASE + (combo // 4)
    layout = combo % 4
    if layout == 0:      # 1=rid, 2=params
        body = pb_varint_field(1, r) + pb_bytes_field(2, payload)
    elif layout == 1:    # 4=params, 5=rid
        body = pb_bytes_field(4, payload) + pb_varint_field(5, r)
    elif layout == 2:    # 3=rid, 4=params
        body = pb_varint_field(3, r) + pb_bytes_field(4, payload)
    else:                # 2=rid, 3=params
        body = pb_varint_field(2, r) + pb_bytes_field(3, payload)
    return frame(ftype, body)

def build_entity_response_candidate(rid, combo):
    """combo 0..4 对应 5 种候选（帧类型, 字段布局）。"""
    payload = _cand_payload(None, None)
    r = int(rid)
    if combo == 0:      # type 5, 4=params 5=rid（镜像 sendEntityMessage）
        body = pb_bytes_field(4, payload) + pb_varint_field(5, r)
        return frame(5, body)
    if combo == 1:      # type 5, 1=rid 2=params
        body = pb_varint_field(1, r) + pb_bytes_field(2, payload)
        return frame(5, body)
    if combo == 2:      # type 6, 4=params 5=rid
        body = pb_bytes_field(4, payload) + pb_varint_field(5, r)
        return frame(6, body)
    if combo == 3:      # type 6, 1=rid 2=params
        body = pb_varint_field(1, r) + pb_bytes_field(2, payload)
        return frame(6, body)
    # combo == 4: type 7, 4=params 5=rid
    body = pb_bytes_field(4, payload) + pb_varint_field(5, r)
    return frame(7, body)

def build_entity_response_via_message(rid, ret=None, resp=None):
    """候选方案 2：把应答塞进 type=2 的 entityMessage，用 field 5 = rid。

    依据：c->s 的 sendEntityMessage 是 1=entityId 3=index 4=params **5=rid**，
    而 s->c 的 entityMessage(entityId, methodName, index, parameters) 我们已知 1/2/3/4。
    所以 field 5 很可能也是 rid；客户端看到 rid != 0 就按应答处理、
    弹 responseManager 的回调，而不是走 CallRpc。
    （独立帧类型 5/6/7/8/9 全试过，客户端都不认。）
    """
    if ret is None:
        ret = {"status": True, "errmsg": ""}
    if resp is None:
        resp = {}
    payload = mp_arr([ret, resp])
    body = pb_bytes_field(4, payload) + pb_varint_field(5, int(rid))
    return frame(2, body)




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

def reframe_as(pkt, new_type):
    """把已有帧的 msgType 换掉（body 不变），用来探测帧类型。"""
    return frame(new_type, pkt[6:])


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
        if total < 6 or n - off < total:
            break
        frames.append(bytes(buf[off:off + total]))
        off += total
    return frames, bytes(buf[off:])


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
# 描述符（扫客户端进程内存拿到的 PropertySyncClientMessage）：
#   ownerid    = 1  bytes
#   propertyid = 2  int64  (required)
#   optype     = 3  int32  (required)
#   name       = 4  bytes  (required)
#   value      = 5  bytes  (msgpack)
#   sessionId  = 6  bytes
#
# 客户端 core/client/gateclientrpchandler.lua:269-288：
#   onSyncProperty(ownerid, propertyid, optype, name, value)
#     -> owner:__syncProperty__(propertyid, optype, name, value, true)
# optype 取值（core/propertysync/propertytypes.lua:27-33）：
#   OP_CHANGE=1  OP_ADD=2  OP_DEL=3  OP_INSERT=4  OP_REMOVE=5
PROPERTY_SYNC_TYPE = 13
# 1-3 里第一个打通写操作链路的 RPC（avataruserdatacomp.lua:348）
RPC_INDEX_SET_SIGNATURE_CS = 2040954396
# 1-2 使用道具（avatarbagcomp.lua:340）
RPC_INDEX_USE_ITEM_CS = 168315907
# 1-1 角色升级（avatarcharactercomp.lua:1015）
RPC_INDEX_UPGRADE_CHARACTER_CS = 732022161
OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3
OP_INSERT = 4
OP_REMOVE = 5


def build_property_sync(ownerid, property_id, op_type, name, value, session_id=""):
    """构造一条 propertySync2Client 帧。

    ownerid 留空字符串表示「就是当前玩家自己」（客户端 onSyncProperty 里
    ownerid ~= "" 才去 getRealOwner）。
    value 用 msgpack 编码；OP_DEL / OP_REMOVE 时传 None。
    """
    body = pb_varint_field(2, int(property_id))
    body += pb_varint_field(3, int(op_type))
    body += pb_bytes_field(4, name.encode("utf-8") if isinstance(name, str) else name)
    if value is not None:
        # "#N" 形式的键要转成整数键（自定义类型里用得多），
        # 否则客户端 __IntTypeKey__ 的容器会收到字符串键。
        body += pb_bytes_field(5, mp_any(_conv_int_keys(value)))
    if ownerid:
        body = pb_bytes_field(1, ownerid.encode("utf-8")
                              if isinstance(ownerid, str) else ownerid) + body
    if session_id:
        body += pb_bytes_field(6, session_id.encode("utf-8"))
    return frame(PROPERTY_SYNC_TYPE, body)

def build_userdata_batch_params(request_params, uid_default=None):
    uid_default = uid_default or _DEF_UID
    """构造 batchGetAttribute 的应答参数（msgpack map）。

    请求结构：parameters = [ list ]，list = [ [uidStr, [attr, ...]], ... ]
    应答结构：{ Ret = [status, errmsg], Results = [ {Uid, AttributesMap} ] }

    只回简单类型的属性（str/int/float/bool）。自定义类型（skins、char_list 等）
    在 initDict 里是 {__tp__, __id__} 这种占位表示，不是客户端要的真实结构，
    硬塞会喂错数据，所以跳过 —— 客户端对缺失属性本来就是容忍的。
    """
    props = AVATAR_INIT_DICT.get("__Properties__", {}) or {}
    reqs = []
    if isinstance(request_params, list) and request_params:
        first = request_params[0]
        if isinstance(first, list):
            reqs = first
    results = []
    for item in reqs:
        if not isinstance(item, list) or len(item) < 2:
            continue
        u = item[0]
        names = item[1] if isinstance(item[1], list) else []
        amap = {}
        for nm in names:
            if not isinstance(nm, str):
                continue
            if nm in props and isinstance(props[nm], _SIMPLE_TYPES):
                amap[nm] = props[nm]
        results.append({"Uid": u if isinstance(u, str) else str(u),
                        "AttributesMap": amap})
    if not results:
        results.append({"Uid": uid_default, "AttributesMap": {}})
    return mp_map({"Ret": [True, 0], "Results": results})

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


def build_entity_mailbox(entity_id=None, server_ip="127.0.0.1",
                         server_port=33101, cluster_id=20000):
    """EntityMailbox { 1:entityId  2:serverIp  3:serverPort  4:clusterId }"""
    entity_id = entity_id or _DEF_ACCOUNT_EID
    body = b""
    if entity_id:
        body += pb_bytes_field(1, entity_id.encode("utf-8"))
    if server_ip:
        body += pb_bytes_field(2, server_ip.encode("utf-8"))
    body += pb_varint_field(3, server_port)
    body += pb_varint_field(4, cluster_id)
    return body


def build_bind_account(auth="", cluster_id=20000, online_info=b"", session_id=None,
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


def frame_bind_account(auth="", cluster_id=20000, online_info=b"", session_id=None,
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


def build_bind_avatar(auth="", cluster_id=20000, online_info=b"", session_id=None,
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


def frame_bind_avatar(auth="", cluster_id=20000, online_info=b"", session_id=None,
                      avt_mailbox=None):
    return frame(BIND_AVATAR_TYPE,
                 build_bind_avatar(auth, cluster_id, online_info, session_id, avt_mailbox))


def build_bind_soul(auth="", session_id=None, soul_mailbox=None,
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


def frame_bind_soul(auth="", session_id=None, soul_mailbox=None,
                    gates=None, cluster_id=20000):
    return frame(BIND_SOUL_TYPE,
                 build_bind_soul(auth, session_id, soul_mailbox, gates, cluster_id))
