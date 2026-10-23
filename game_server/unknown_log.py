#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""未实现请求的落盘 + 聚合（雷2）。

为什么要它
----------
以前未实现的 RPC / 微服务方法只有一个通用兜底：回 `{status:true}`。
结果是「客户端以为成功、服务端什么都没做」——邮件附件被本地标记已领、
奖励永久丢失就是这条路。而且**没人知道到底缺了什么**。

现在：
  - 每次未实现请求都记一行（kind / key / uid / 模式 / 回包 / 参数样例）
  - 同一 key 5 秒内只写一行原始记录（其余只累加计数），避免重演 hexdump 刷屏
  - 文件超过 8 MB 自动轮转成 .1
  - 内存里聚合 {key: 次数/首次/末次}，供工具和测试读取

文件位置：config.UNKNOWN_LOG_PATH（默认 work/logs/unknown_requests.tsv）
"""
import os
import threading
import time

from . import config

_LOCK = threading.RLock()
_STATS = {}                 # (kind, key) -> {kind,key,count,first,last,sample}
_LAST_RAW = {}              # (kind, key) -> 上次写原始行的时间
_RAW_INTERVAL = 5.0         # 同 key 原始行最小间隔（秒）
_MAX_BYTES = 8 * 1024 * 1024


def _sample(params):
    """参数样例：只留前 64 字节，避免把日志写爆。"""
    if params is None:
        return ""
    if isinstance(params, (bytes, bytearray)):
        b = bytes(params[:64])
        return b.hex()
    s = repr(params)
    return s[:120]


def _rotate_if_needed(p):
    try:
        if os.path.getsize(p) <= _MAX_BYTES:
            return
        bak = p + ".1"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(p, bak)
    except OSError:
        pass


def _append(kind, key, uid, fake_ok, reply, sample, now):
    p = config.UNKNOWN_LOG_PATH
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        _rotate_if_needed(p)
        new = not os.path.exists(p) or os.path.getsize(p) == 0
        with open(p, "a", encoding="utf-8", newline="") as f:
            if new:
                f.write("ts\tkind\tkey\tuid\tmode\treply\tsample\n")
            f.write("%d\t%s\t%s\t%s\t%s\t%s\t%s\n" % (
                int(now), kind, key, uid or "-",
                "compat" if fake_ok else "strict", reply, sample))
    except OSError:
        pass


def record(kind, key, uid=None, params=None, fake_ok=False, reply=""):
    """记录一次未实现请求，返回该 key 的累计次数。"""
    now = time.time()
    k = (kind, key)
    with _LOCK:
        st = _STATS.get(k)
        if st is None:
            st = {"kind": kind, "key": key, "count": 0,
                  "first": now, "last": now, "sample": _sample(params),
                  "uid": uid or ""}
            _STATS[k] = st
        st["count"] += 1
        st["last"] = now
        if not st["sample"]:
            st["sample"] = _sample(params)
        if now - _LAST_RAW.get(k, 0) >= _RAW_INTERVAL:
            _LAST_RAW[k] = now
            _append(kind, key, uid, fake_ok, reply, st["sample"], now)
        return st["count"]


def stats():
    """聚合快照（按次数降序）。"""
    with _LOCK:
        rows = [dict(v) for v in _STATS.values()]
    rows.sort(key=lambda r: -r["count"])
    return rows


def reset():
    """清空内存聚合（测试用；不动文件）。"""
    with _LOCK:
        _STATS.clear()
        _LAST_RAW.clear()
