#!/usr/bin/env python3
"""game_server.server —— 游戏服入口（微服务网关 + 游戏网关）。

监听:
    33100  微服务网关（ClientGate.syncMetadata / GameLoadService.query）
    33101  游戏网关（connectServer / connectResponse / bindAccount / bindAvatar）
    33102  本地管理接口（admin_api，只监听 127.0.0.1）

常用模式:
  ★ 正式联调（推荐）
      python -m game_server.server --ms-ok
      33100 回正确的 MsResponse，33101 回 ConnectServerResp，
      并按 reqType 0 -> 4 -> 3 推进绑定链（bindAccount -> bindAvatar）。

  纯抓包（看客户端发什么，什么都不回）
      python -m game_server.server

  反代抓包（把流量原样转发给官方网关，观察官方应答）
      python -m game_server.server --proxy <官方网关 host:port>

  回显
      python -m game_server.server --echo

分层（P0 遗留清理后）：
    server.py                  本文件：解析命令行 + 起监听 + 分发
    transport/gate.py          游戏网关连接（绑定链 + 实体 RPC）
    transport/ms_service.py    微服务连接（query / batchGetAttribute）
    transport/capture.py       抓包 / 代理 / 回显
    transport/accounts.py      多账号 gate 连接 <-> uid 关联
    transport/net.py           帧收发、抓包日志、连接序号

记录内容:
  * 连接建立/断开（客户端地址、持续时间、上游地址）
  * 每次 recv 的：相对时间(ms)、方向(C=客户端发来 / S=服务端发来)、字节数、十六进制、ASCII
  * 原始字节流另存到 work/captures/conn_*.bin
"""
import contextlib
import os
import socket
import sys
import threading

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db  # noqa: E402
from shared.log_tee import get_logger

from . import config as ooconf  # noqa: E402
from .transport.capture import handle_capture  # noqa: E402
from .transport.gate import handle_gate  # noqa: E402
from .transport.ms_service import handle_ms  # noqa: E402
from .transport.net import CAPTURE_DIR, next_cid  # noqa: E402

_log = get_logger(__name__)

# ★ 端口/路径一律从 game_server.config 取，不要在这里写死。
HOST = ooconf.HOST
PORT = ooconf.MS_PORT
MS_PORT = ooconf.MS_PORT
GATE_PORT = ooconf.GATE_PORT

ECHO = False
UPSTREAM = None          # (host, port)
MS_MODE = False


def _listen(host, port):
    """起一个监听 socket（SO_REUSEADDR，避免重启时 TIME_WAIT 卡端口）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    return srv


def _serve_capture(host=None, port=None):
    """抓包/代理/回显模式的监听（非正式联调路径）。"""
    db.init()
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    host = host or HOST
    port = port or PORT
    srv = _listen(host, port)
    if UPSTREAM:
        mode = "proxy->{}:{}".format(*UPSTREAM)
    else:
        mode = "echo" if ECHO else "capture-only"
    _log.info(f'[game] listening on {host}:{port} ({mode})')
    _log.info(f"[game] captures -> {CAPTURE_DIR}")
    return srv


def _accept_loop(srv, handler):
    """接一条连接开一个线程。handler(conn, addr, cid)。"""
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handler, args=(conn, addr, next_cid()),
                         daemon=True).start()


def _run_ms_mode():
    """正式联调：33100 微服务 + 33101 游戏网关。"""
    db.init()
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    ms_srv = _listen("0.0.0.0", MS_PORT)
    gate_srv = _listen("0.0.0.0", GATE_PORT)
    _log.info(f'[ms] ms service  listening on 0.0.0.0:{MS_PORT}')
    _log.info(f'[ms] gate listening on 0.0.0.0:{GATE_PORT}')
    _log.info(f"[ms] captures -> {CAPTURE_DIR}")

    threading.Thread(target=_accept_loop, args=(gate_srv, handle_gate),
                     daemon=True).start()
    try:
        _accept_loop(ms_srv, handle_ms)
    except KeyboardInterrupt:
        _log.info("[ms] shutting down")


def _configure_scaffold(argv):
    """脚手架开关（默认全关，走和真实游戏一致的开局）。

    见 game_server/config.py 的 Scaffold 注释与 game_server/scaffold.py。
    """
    unlock_all = "--unlock-all" in argv
    level = None
    tasks_done = None
    for i, a in enumerate(argv):
        if a == "--level" and i + 1 < len(argv):
            try:
                level = int(argv[i + 1])
            except Exception:
                _log.info("[cfg] --level 需要整数")
        if a == "--tasks-done":
            tasks_done = True
        if a == "--no-tasks-done":
            tasks_done = False
    if "--no-scaffold" in argv:
        unlock_all = False
        level = None
        tasks_done = False
    sc = ooconf.configure_scaffold(unlock_all=unlock_all, level=level,
                                   all_tasks_done=tasks_done)
    _log.info(f"[cfg] 脚手架: {sc.describe()}")


def _configure_dungeon(argv):
    """--dungeon-last-only：只下发最后一关（测试结算不用每次打三关）。

    序章有 3 关（50001 天台 / 50002 千相会 / 50003 千手Boss战）。
    """
    last_only = "--dungeon-last-only" in argv
    if os.environ.get("OO_DUNGEON_LAST_ONLY", "") not in ("", "0"):
        last_only = True
    ooconf.configure_dungeon_last_only(last_only)
    if last_only:
        _log.info("[cfg] 副本: 只下发最后一关（一关直达 Boss）")


def _start_admin():
    """本地管理接口（给网页命令系统用）。

    网页端（8399）和游戏服（33100）是两个进程，命令要改游戏服状态，
    走这个显式的 HTTP 边界，而不是共用一个 SQLite 表互相猜。
    只监听 127.0.0.1，需要 X-OO-Token。
    """
    try:
        from . import admin_api as _admin_api
        _admin_api.start()
        print(f'[admin] 管理接口 http://127.0.0.1:{_admin_api.port()}（命令系统用）', flush=True)
    except Exception as _e:
        _log.warning(f"[admin] 管理接口启动失败: {_e}")


def main():
    global ECHO, UPSTREAM, MS_MODE
    # ★ 统一日志：文件双写（本次运行 + last）+ 控制台，print 原样保留
    #   拦截器按行缓冲，自动规避编码异常（errors=replace 由 FileHandler 兜底）
    try:
        from shared.log_tee import install as _log_install
        _log_install("game", ooconf.LOG_DIR)
    except Exception as _e:
        # 日志装不上不能挡启动（极端只读环境）：退回老的编码兜底
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        _log.warning(f"[log] 统一日志安装失败，退回控制台模式: {_e}")

    argv = sys.argv[1:]
    if "--echo" in argv:
        ECHO = True
    if "--ms-ok" in argv or "--ms" in argv:
        # ms 协议已完全解出（见 game_server/ms/ 头注释），监听一律走正确应答；
        # --ms-ok 是正式参数，--ms 是它的旧别名（外部脚本可能还在用）。
        MS_MODE = True
    for i, a in enumerate(argv):
        if a == "--proxy" and i + 1 < len(argv):
            hp = argv[i + 1]
            h, _, p = hp.rpartition(":")
            UPSTREAM = (h, int(p))

    # ★ 雷2：未实现请求的处理模式（严格 = 回失败；兼容 = 回成功）
    if "--unknown-ok" in argv:
        ooconf.configure_unknown_fake_ok(True)
        print("[cfg] 未实现请求: 兼容模式（回成功；仍记入 unknown_requests.tsv）",
              flush=True)
    else:
        print("[cfg] 未实现请求: 严格模式（回失败；记入 unknown_requests.tsv）",
              flush=True)

    _configure_scaffold(argv)
    _configure_dungeon(argv)
    _start_admin()

    if MS_MODE:
        _run_ms_mode()
        return

    srv = _serve_capture()
    try:
        _accept_loop(
            srv, lambda c, a, i: handle_capture(c, a, i, ECHO, UPSTREAM))
    except KeyboardInterrupt:
        _log.info("[game] shutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
