#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web.ratelimit —— 登录失败限速（P0-E3 从 web/app.py 抽出）。

同一账号 10 分钟内失败 5 次即暂时拒绝，防止在线爆破。
只放内存，进程重启即清零 —— 私服场景够用，不必落库。
"""
import threading
import time

LOGIN_WINDOW = 600
LOGIN_MAX_FAIL = 5

_lock = threading.Lock()
_fails = {}


def blocked(username):
    """该账号现在是否处于「失败过多，暂时拒绝」状态。"""
    now = time.time()
    with _lock:
        arr = [t for t in _fails.get(username, []) if now - t < LOGIN_WINDOW]
        _fails[username] = arr
        return len(arr) >= LOGIN_MAX_FAIL


def note_fail(username):
    with _lock:
        _fails.setdefault(username, []).append(time.time())


def note_ok(username):
    with _lock:
        _fails.pop(username, None)
