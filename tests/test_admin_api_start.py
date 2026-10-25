#!/usr/bin/env python
"""管理接口启动路径回归测试（不传 port 的那条）。

背景（2026-10-25）：D5 把 admin_api._port() 重命名成 port() 后，
start(host, port=None) 的参数名把模块级 port() 遮蔽了，
`p = port or port()` 变成 `None or None()` -> TypeError，
管理接口在真实启动路径（server.py 里 start() 无参调用）静默起不来。
65 个套件全绿没拦住 —— 因为唯一调用它的测试显式传了 port=。

本测试锁死：start() 不传 port 也必须起得来。

用法: python tests/test_admin_api_start.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_adminboot_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_adminboot_db_"), "data.db")
os.environ["OO_ADMIN_PORT"] = "0"      # 0 = 临时端口，不抢 33102
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import admin_api  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] start() 不传 port —— 真实启动路径")
    try:
        srv, th = admin_api.start()
    except Exception as e:
        check("start() 返回 (server, thread)", False, f"{type(e).__name__}: {e}")
        return 1
    import threading as _threading
    check("start() 返回 (server, thread)",
          hasattr(srv, "server_port") and isinstance(th, _threading.Thread),
          (type(srv).__name__, type(th).__name__))
    try:
        port = srv.server_port
        check("绑到临时端口", port > 0, port)
        check("线程存活", th.is_alive(), th.is_alive())
        check("port() 不受遮蔽影响", admin_api.port() == 0, admin_api.port())
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n[2] start(port=0) 显式临时端口（不能把 0 当「未提供」）")
    srv2, _th2 = admin_api.start(port=0)
    try:
        check("显式 0 也绑得起来", srv2.server_port > 0, srv2.server_port)
    finally:
        srv2.shutdown()
        srv2.server_close()

    print("\n[3] _resolve_port 语义")
    check("None -> port() 的结果", admin_api._resolve_port(None) == 0,
          admin_api._resolve_port(None))
    check("显式数字原样返回", admin_api._resolve_port(12345) == 12345)
    check("字符串数字可转", admin_api._resolve_port("12345") == 12345)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
