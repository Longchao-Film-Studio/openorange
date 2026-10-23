#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""管理接口口令（雷3）回归测试。

背景：admin_api 原来写死 DEFAULT_TOKEN="local-dev"，启动脚本也不覆盖 ——
同一台机器上任何进程都能 POST /player/grant 发任意物品。
现在口令解析顺序：OO_ADMIN_TOKEN -> work/admin_token.txt -> 自动生成并落盘。

用法: python tests/test_admin_token.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离：不碰真实 work/admin_token.txt
_TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="oo_test_admintok_"), "admin_token.txt")
os.environ["OO_ADMIN_TOKEN_FILE"] = _TOKEN_FILE
os.environ.pop("OO_ADMIN_TOKEN", None)
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_admintok_state_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_admintok_db_"), "data.db")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import admin_api  # noqa: E402
from shared import admin_token  # noqa: E402
from shared import commands as cmds  # noqa: E402
from shared.commands import backend  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


class _Echo(BaseHTTPRequestHandler):
    """记录收到的 X-OO-Token，回 {"ok":true}。"""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Echo.seen.append(self.headers.get("X-OO-Token"))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    print("\n[1] 环境变量优先")
    os.environ["OO_ADMIN_TOKEN"] = "env-token-123"
    admin_token.reset_cache()
    check("返回环境变量值", admin_token.get_token() == "env-token-123",
          admin_token.get_token())

    print("\n[2] 没有环境变量时读文件")
    os.environ.pop("OO_ADMIN_TOKEN", None)
    admin_token.reset_cache()
    with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write("file-token-abc\n")
    check("返回文件里的口令", admin_token.get_token() == "file-token-abc",
          admin_token.get_token())

    print("\n[3] 都没有时自动生成并落盘（不再有公开默认值）")
    os.remove(_TOKEN_FILE)
    admin_token.reset_cache()
    tok = admin_token.get_token()
    check("自动生成长度 >= 20", isinstance(tok, str) and len(tok) >= 20, len(tok or ""))
    check("★ 不再是 local-dev", tok != "local-dev", tok)
    check("已写入文件", os.path.exists(_TOKEN_FILE), _TOKEN_FILE)
    check("二次调用稳定", admin_token.get_token() == tok)
    admin_token.reset_cache()
    check("清缓存后从文件读回同一值", admin_token.get_token() == tok,
          (admin_token.get_token(), tok))

    print("\n[4] admin_api 用同一套解析")
    check("admin_api._token() == admin_token.get_token()",
          admin_api._token() == admin_token.get_token(),
          (admin_api._token(), admin_token.get_token()))
    check("模块里没有 DEFAULT_TOKEN 常量",
          not hasattr(admin_api, "DEFAULT_TOKEN"))

    print("\n[5] backend 调管理接口时带上解析出的口令（端到端）")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # ★ admin_call 从**包命名空间**取 ADMIN_URL/ADMIN_TOKEN
        #   （见 backend.py 注释：这样 tests 里 cmds.ADMIN_URL = ... 才生效）
        old_url, old_tok = cmds.ADMIN_URL, cmds.ADMIN_TOKEN
        cmds.ADMIN_URL = "http://127.0.0.1:%d" % port
        cmds.ADMIN_TOKEN = None             # 强制走 admin_token.get_token()
        backend.admin_call("GET", "/health")
        check("服务端收到自动生成的口令",
              _Echo.seen and _Echo.seen[-1] == admin_token.get_token(),
              _Echo.seen[-1])
        check("不是 local-dev", _Echo.seen[-1] != "local-dev", _Echo.seen[-1])
        cmds.ADMIN_TOKEN = "explicit-token"
        backend.admin_call("GET", "/health")
        check("显式 ADMIN_TOKEN 覆盖生效", _Echo.seen[-1] == "explicit-token",
              _Echo.seen[-1])
        cmds.ADMIN_URL, cmds.ADMIN_TOKEN = old_url, old_tok
    finally:
        srv.shutdown()

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
