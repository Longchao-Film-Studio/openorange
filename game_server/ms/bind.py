#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绑定账号 / Avatar / Soul 的帧（P0-E3 从 ms.py 拆出）。

登录链路：connectServer -> bindAccount(6) -> bindAvatar(7) -> bindSoul(8)。
每种绑定都带一个 mailbox（server_ip + port + entity_id），客户端用它做后续路由。
"""
from .frames import frame
from .defaults import _DEF_ACCOUNT_EID
from .protocol import mp_arr, mp_map, pb_bytes_field, pb_varint_field
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
