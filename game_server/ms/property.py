#!/usr/bin/env python3
"""属性同步帧（P0-E3 从 ms.py 拆出）。

propertySync2Client 的帧类型是 13，载荷是 protobuf：
  1 = ownerid  2 = propertyId  3 = opType  4 = name  5 = value(msgpack)

★ opType 语义（客户端 puppetpropertycomponent.lua）：
  OP_CHANGE 改已有值 / OP_ADD 新增子项 / OP_DEL 删子项 /
  OP_INSERT 列表插入 / OP_REMOVE 列表删除
  新键必须用 OP_ADD，否则客户端 _changeSubProperty 里 oldVal 为 nil 会崩。
"""
from .frames import frame
from .protocol import conv_int_keys, mp_any, pb_bytes_field, pb_varint_field

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
        body += pb_bytes_field(5, mp_any(conv_int_keys(value)))
    if ownerid:
        body = pb_bytes_field(1, ownerid.encode("utf-8")
                              if isinstance(ownerid, str) else ownerid) + body
    if session_id:
        body += pb_bytes_field(6, session_id.encode("utf-8"))
    return frame(PROPERTY_SYNC_TYPE, body)
