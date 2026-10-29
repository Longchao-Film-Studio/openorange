#!/usr/bin/env python3
"""微服务连接的进程内表（cid -> socket，uid -> cid）。

## 为什么需要它

`MicroPush`（帧类型 4）是**服务端主动**发的 —— 要让"给某个玩家推一条消息"这件事
有地方可查：他的 MS 连接是哪条、还活着吗。

实体通道已经有 `transport/online.py`，但邮件推送走的是**微服务通道**
（客户端的 `MailService` 在那边），两条连接是两个 socket，不能混用。

## 并发纪律（与 online.py 一致，踩过才写下来的）

1. 全局 `_LOCK` **只保护表结构**，绝不在锁内做 socket I/O。
2. 同一个 socket 会被两个线程写：
     · `handle_ms` 线程：回 MsResponse
     · 业务线程：推 MicroPush（新邮件）
   不加锁时两段字节会交错，客户端按 [len][type][payload] 解析直接崩。
   所以**所有**发送都必须走 `send_frame()`，不能直接 `conn.sendall()`。

## uid 从哪来

客户端在 `ClientGate.bind`（msproxy4client.lua:14）里把自己的 `msCallerId`（= uid）
发上来，服务端在应答前调 `bind_uid(cid, uid)` 建立映射。
在此之前这条连接没有身份，推送找不到它 —— 这是正常的：不 bind 就不推送。
"""
from __future__ import annotations

import threading
import time

from shared.log_tee import get_logger

_log = get_logger(__name__)

_LOCK = threading.Lock()
#: cid -> _Conn
_ENTRIES: dict[int, _Conn] = {}
#: uid -> cid（一个账号同一时刻只保留最新的那条 MS 连接）
_BY_UID: dict[str, int] = {}


class _Conn:
    __slots__ = ("cid", "conn", "frames_sent", "opened_at", "send_lock", "uid")

    def __init__(self, cid, conn):
        self.cid = cid
        self.conn = conn
        self.uid: str | None = None
        self.send_lock = threading.Lock()
        self.opened_at = time.time()
        self.frames_sent = 0

    def __repr__(self):
        return f"<MsConn cid={self.cid} uid={self.uid} sent={self.frames_sent}>"


def register(cid, conn):
    """连接建立时登记（此时还不知道 uid）。"""
    with _LOCK:
        e = _ENTRIES.get(cid)
        if e is not None:
            return e
        e = _Conn(cid, conn)
        _ENTRIES[cid] = e
        return e


def unregister(cid):
    """连接断开时摘除（幂等）。"""
    with _LOCK:
        e = _ENTRIES.pop(cid, None)
        if e is None:
            return False
        if e.uid and _BY_UID.get(e.uid) == cid:
            _BY_UID.pop(e.uid, None)
        return True


def bind_uid(cid, uid):
    """`ClientGate.bind` 成功后调用：把这条连接认到某个账号上。

    同一个账号重连时，旧 cid 的映射被顶掉（新连接赢），旧连接断开时会自然清理。
    """
    if not uid:
        return False
    with _LOCK:
        e = _ENTRIES.get(cid)
        if e is None:
            return False
        e.uid = str(uid)
        _BY_UID[str(uid)] = cid
        return True


def unbind_cid(cid):
    """解除这条连接与账号的映射（客户端 unbind 时调；断开时 unregister 也会清）。"""
    with _LOCK:
        e = _ENTRIES.get(cid)
        if e is None or not e.uid:
            return False
        if _BY_UID.get(e.uid) == cid:
            _BY_UID.pop(e.uid, None)
        e.uid = None
        return True


def uid_of(cid):
    with _LOCK:
        e = _ENTRIES.get(cid)
        return e.uid if e is not None else None


def cid_of(uid):
    with _LOCK:
        return _BY_UID.get(str(uid))


def send_frame(cid, frame):
    """给一条 ms 连接发一帧（持该连接的 send_lock）。返回 True=成功。

    回 MsResponse 和推 MicroPush 都走这里 —— 这是唯一的写 socket 入口。
    """
    if not frame:
        return True
    with _LOCK:
        e = _ENTRIES.get(cid)
    if e is None:
        return False
    try:
        with e.send_lock:
            e.conn.sendall(frame)
            e.frames_sent += 1
        return True
    except Exception as ex:                      # 对端已断：摘掉，别留死连接
        _log.warning(f"[msconn] cid={cid} 发送失败，摘除: {ex}")
        unregister(cid)
        return False


def push(uid, frame):
    """给某个账号的 ms 连接推一帧。不在线返回 False（调用方不应视为错误）。"""
    cid = cid_of(uid)
    if cid is None:
        return False
    ok = send_frame(cid, frame)
    if ok:
        _log.info(f"[msconn] 推送 uid={uid} cid={cid} {len(frame)}B")
    return ok


def count():
    with _LOCK:
        return len(_ENTRIES)


def bound_uids():
    with _LOCK:
        return sorted(_BY_UID)


def reset_for_tests():
    """测试用：清空表（不关连接）。"""
    with _LOCK:
        _ENTRIES.clear()
        _BY_UID.clear()
