#!/usr/bin/env python3
"""web.ratelimit —— 登录失败限速（P0-E3 从 web/app.py 抽出）。

同一账号 10 分钟内失败 5 次即暂时拒绝，防止在线爆破。
只放内存，进程重启即清零 —— 私服场景够用，不必落库。
"""
import threading
import time

LOGIN_WINDOW = 600
LOGIN_MAX_FAIL = 5

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}
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


# ---------------------------------------------------------------- 通用限流
# ★ 雷6（2026-10-23）：游戏内指令通道（/api/chat/command）按账号限流。
#   票据不是一次性的（客户端会话内一直复用），所以用「窗口内次数」挡滥用：
#   既保留"一条票据可执行多条命令"的现有行为，又限制被泄露票据的破坏速度。
_RATE: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()

CHAT_WINDOW = 60      # 秒
CHAT_MAX = 30         # 每窗口最多命令数


def rate_allow(key, limit=None, window=None, now=None):
    """窗口内是否放行（放行时计数 +1）。"""
    limit = CHAT_MAX if limit is None else int(limit)
    window = CHAT_WINDOW if window is None else float(window)
    now = time.time() if now is None else float(now)
    with _RATE_LOCK:
        arr = [t for t in _RATE.get(key, []) if now - t < window]
        if len(arr) >= limit:
            _RATE[key] = arr
            return False
        arr.append(now)
        _RATE[key] = arr
        return True


def reset_rate():
    """测试用：清空限流表。"""
    with _RATE_LOCK:
        _RATE.clear()
