#!/usr/bin/env python3
"""本地管理接口的口令（雷3）。

背景
----
admin_api 只监听 127.0.0.1，但原来 DEFAULT_TOKEN = "local-dev" 写死在代码里，
启动脚本也不覆盖 —— 同一台机器上任何进程都能 POST /player/grant 发任意物品。
"打印一句警告"不足以消除这个洞，所以改成：

  1. 环境变量 OO_ADMIN_TOKEN 优先（运维显式指定）
  2. 否则读 work/admin_token.txt（两个进程共用同一份）
  3. 都没有就**自动生成**一个高熵口令并原子写入该文件（O_EXCL，避免竞态）

这样 web（8399）和 game_server（33102）两个进程不设环境变量也能自动对齐口令，
默认口令 "local-dev" 彻底消失。
"""
import os
import secrets
import sys
import threading

_LOCK = threading.RLock()
_CACHED = None
_DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "work", "admin_token.txt")
_WARNED = False


def token_file():
    return os.environ.get("OO_ADMIN_TOKEN_FILE") or _DEFAULT_FILE


def _warn():
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    try:
        print(f"[admin] 警告：未设置 OO_ADMIN_TOKEN，使用自动生成口令（{token_file()}）。", file=sys.stderr, flush=True)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass


def _read_file(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_file_atomic(p, token):
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return _read_file(p)
    except OSError:
        return None
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return token


def get_token():
    """取本地管理接口口令（永远不会返回 None）。"""
    global _CACHED
    env = os.environ.get("OO_ADMIN_TOKEN")
    if env:
        return env
    with _LOCK:
        if _CACHED:
            return _CACHED
        p = token_file()
        tok = _read_file(p)
        if not tok:
            tok = _write_file_atomic(p, secrets.token_urlsafe(24))
        if not tok:
            # 文件不可写（只读部署）：退化为进程内随机口令，至少不再是公开值
            tok = secrets.token_urlsafe(24)
        _CACHED = tok
        _warn()
        return tok


def reset_cache():
    """测试用：清掉进程内缓存（不删文件）。"""
    global _CACHED
    with _LOCK:
        _CACHED = None
