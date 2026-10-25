#!/usr/bin/env python3
"""在线连接表（cid -> 游戏连接）—— 服务端主动推送的唯一入口。

★ 为什么需要这张表（2026-10-26）
  gate 是 thread-per-connection：每条连接只有自己的局部变量，没有任何全局视图。
  结果就是服务端**只能回包，不能主动推送**——「全服公告」「踢人」「GM 喊话」
  这类消息一条都发不出去（S2C 147 条里我们只发了 9 条）。
  要发广播，先得能拿到「所有在线连接」。

  注意与 shared/db.py 的 game_online 区分：
    DB 那张表是**落库标记**（给网页看谁在线、带 stale 超时），拿不到 socket；
    这张表是**进程内活连接**，能真正 sendall。两者互补，不要合并。

★ 并发模型（两条规则，踩过才写下来的）
  1) 全局 _LOCK **只保护表结构**，绝不在锁内做 socket I/O
     —— 与 player_state / dispatch 的既有约定一致（锁内只改状态 + 造帧）。
  2) 每条连接自带一把 send_lock：同一个 socket 会被两个线程写——
       · gate 线程：回包（心跳应答 / RPC 应答）
       · 广播线程：全服公告 / 踢人
     不加锁时两段字节会交错，客户端按 [len][type][payload] 解析直接崩。
     所以**所有**发送都必须走 online.send()，不能直接 conn.sendall()。

★ 表里存的是**所有**连接（含未登录的），靠 uid 字段区分：
  未登录 = config.FALLBACK_UID（"OO00000001"），广播时跳过。
  这样 gate 在登录前后都能安全地把发送统一走这里，不用判断"注册了没"。
"""
import threading
import time

from shared.log_tee import get_logger

from ..config import FALLBACK_UID

_log = get_logger(__name__)

_LOCK = threading.Lock()

# cid -> _Conn
_ENTRIES = {}
# uid -> {cid, ...}（同一个账号可能有多条连接：顶号前的旧连接、多开）
_BY_UID = {}


class _Conn:
    """一条在线连接的登记项。"""

    __slots__ = ("cid", "conn", "frames_sent", "opened_at", "send_lock", "uid")

    def __init__(self, cid, conn):
        self.cid = cid
        self.conn = conn
        self.uid = None
        self.opened_at = time.time()
        self.send_lock = threading.Lock()
        self.frames_sent = 0

    def __repr__(self):
        return f"<Conn cid={self.cid} uid={self.uid} sent={self.frames_sent}>"


def register(cid, conn):
    """连接建立时登记（此时还不知道 uid）。"""
    with _LOCK:
        if cid in _ENTRIES:
            return _ENTRIES[cid]
        e = _Conn(cid, conn)
        _ENTRIES[cid] = e
        return e


def unregister(cid):
    """连接断开时摘除（幂等）。"""
    with _LOCK:
        e = _ENTRIES.pop(cid, None)
        if e is None:
            return False
        if e.uid:
            s = _BY_UID.get(e.uid)
            if s is not None:
                s.discard(cid)
                if not s:
                    _BY_UID.pop(e.uid, None)
        return True


def set_uid(cid, uid):
    """设置/更新这条连接的账号（登录后 uid 会从 FALLBACK 变成真实账号）。

    幂等：重复调用同值无副作用；换账号时把索引也搬过去。
    """
    if uid is None:
        return
    with _LOCK:
        e = _ENTRIES.get(cid)
        if e is None:
            return
        if e.uid == uid:
            return
        if e.uid:
            s = _BY_UID.get(e.uid)
            if s is not None:
                s.discard(cid)
                if not s:
                    _BY_UID.pop(e.uid, None)
        e.uid = uid
        if uid != FALLBACK_UID:
            _BY_UID.setdefault(uid, set()).add(cid)


def send(cid, frames):
    """给**一条**连接发一组帧（整组一次 sendall，避免半帧）。

    frames: list[bytes]；返回 True=成功。
    ★ 所有发送都应走这里：只有这里持有 send_lock，能和广播线程互斥。
    """
    if not frames:
        return True
    payload = b"".join(frames)
    with _LOCK:
        e = _ENTRIES.get(cid)
    if e is None:
        return False
    try:
        with e.send_lock:
            e.conn.sendall(payload)
            e.frames_sent += len(frames)
        return True
    except Exception as ex:                      # 对端已断：摘掉，别让表里留死连接
        _log.warning(f"[online] cid={cid} 发送失败，摘除: {ex}")
        unregister(cid)
        return False


def send_to(uid, frames):
    """给某个账号的**所有**连接发（顶号/多开场景）。

    返回 (成功条数, 失败条数)。
    """
    if not uid or not frames:
        return (0, 0)
    with _LOCK:
        cids = list(_BY_UID.get(uid, ()))
    ok = fail = 0
    for cid in cids:
        if send(cid, frames):
            ok += 1
        else:
            fail += 1
    return (ok, fail)


def broadcast(frames, exclude_cid=None, real_only=True):
    """给所有在线连接广播一组帧。返回统计 dict。

    real_only: True 时跳过未登录连接（uid 仍是 FALLBACK_UID）——
               这是公告/踢人的默认行为，别把消息发给还在登录界面的连接。
    """
    if not frames:
        return {"targets": 0, "ok": 0, "fail": 0}
    with _LOCK:
        targets = [
            e.cid for e in _ENTRIES.values()
            if e.cid != exclude_cid
            and (not real_only or (e.uid and e.uid != FALLBACK_UID))
        ]
    ok = fail = 0
    for cid in targets:
        if send(cid, frames):
            ok += 1
        else:
            fail += 1
    if targets:
        _log.info(f"[online] 广播 {len(targets)} 条连接：成功 {ok} / 失败 {fail}")
    return {"targets": len(targets), "ok": ok, "fail": fail}


def broadcast_s2c(method_name, index, params=None, exclude_cid=None, real_only=True):
    """广播一条 S2C 实体消息（服务端 -> 客户端）。

    契约见 docs/S2C接收端地图.md：
      · index 必须是 genRpcIndex(名字)，与 rpc_list.tsv 一致
      · params 必须匹配 config.lua 的 ServerOnlyMsg 声明
      · 客户端不认识的名字不会崩，只打 error 日志（安全失败）
    这里延迟 import ms，避免模块级循环依赖。
    """
    from .. import ms as msmod
    pkt = msmod.build_entity_message(
        entity_id="", method_name=method_name, index=index,
        params=msmod.mp_arr(params or []))
    return broadcast([pkt], exclude_cid=exclude_cid, real_only=real_only)


def count(real_only=False):
    """在线连接数（real_only=True 只算已登录的）。"""
    with _LOCK:
        if not real_only:
            return len(_ENTRIES)
        return len(_BY_UID)


def uids():
    """当前已登录账号列表（去重）。"""
    with _LOCK:
        return sorted(_BY_UID.keys())


def snapshot():
    """排障用快照：[{cid, uid, opened_at, frames_sent}]。"""
    now = time.time()
    with _LOCK:
        return [
            {"cid": e.cid, "uid": e.uid,
             "opened_sec": round(now - e.opened_at, 1),
             "frames_sent": e.frames_sent}
            for e in _ENTRIES.values()
        ]


def reset_for_tests():
    """测试用：清空表（不关连接）。"""
    with _LOCK:
        _ENTRIES.clear()
        _BY_UID.clear()
