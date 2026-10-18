#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools.run_all —— 一键启动全部服务

用法:  python tools/run_all.py

启动:
  auth_server  0.0.0.0:8443   (假 fp-passport, HTTPS)
  web          0.0.0.0:8322   (验证码展示页)
  game_server  0.0.0.0:33100  (游戏服占位, TCP)
  serverlist   0.0.0.0:8080   (假服务器列表 -> 指向本地 game_server)
  proxy        0.0.0.0:4430   (MITM 代理)   [--proxy 开启]
"""
import os
import signal
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db  # noqa: E402


def _serve_in_thread(httpd):
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


def main():
    db.init()
    print("=" * 56, flush=True)
    print(" OpenOrange —— 归龙潮私服", flush=True)
    print("=" * 56, flush=True)

    # 1) 认证服
    from auth_server import server as auth_server
    try:
        ah = auth_server.serve()
        _serve_in_thread(ah)
    except OSError as e:
        print("[run] 认证服启动失败(端口占用?):", e, flush=True)

    # 2) Web 验证码面板
    from web import app as web_app
    try:
        wh = web_app.serve()
        _serve_in_thread(wh)
    except OSError as e:
        print("[run] Web 启动失败(端口占用?):", e, flush=True)

    # 3) 游戏服(占位, TCP)
    from game_server import server as game_server
    _game_sock = None
    try:
        _game_sock = game_server.serve()

        def _accept_loop():
            import threading as _th
            from game_server.server import _handle
            while True:
                try:
                    conn, addr = _game_sock.accept()
                except OSError:
                    break
                _th.Thread(target=_handle, args=(conn, addr), daemon=True).start()

        threading.Thread(target=_accept_loop, daemon=True).start()
    except OSError as e:
        print("[run] 游戏服启动失败(端口占用?):", e, flush=True)

    # 4) 服务器列表
    from serverlist_server import server as serverlist
    try:
        sh = serverlist.serve()
        _serve_in_thread(sh)
    except OSError as e:
        print("[run] 服务器列表启动失败(端口占用?):", e, flush=True)

    # 5) 代理(可选)
    if "--proxy" in sys.argv:
        from tools import proxy as proxy_mod
        try:
            ph = proxy_mod.serve()
            _serve_in_thread(ph)
        except OSError as e:
            print("[run] 代理启动失败:", e, flush=True)

    print("", flush=True)
    print("-" * 56, flush=True)
    print(" 认证服   https://0.0.0.0:8443", flush=True)
    print(" 验证码页 http://0.0.0.0:8322   <-- 玩家看这里", flush=True)
    print(" 游戏服   0.0.0.0:33100 (占位)", flush=True)
    print(" 服务器列表 0.0.0.0:8080", flush=True)
    if "--proxy" in sys.argv:
        print(" 代理     0.0.0.0:4430", flush=True)
    print("-" * 56, flush=True)
    print(" Ctrl+C 退出", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run] 正在退出...", flush=True)


if __name__ == "__main__":
    main()
