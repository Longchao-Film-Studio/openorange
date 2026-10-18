#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理控制台端到端测试。

用法:
    python tests/test_admin_console.py

会临时起一个 web.app（随机端口 + 临时数据库），跑完自动清理。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLAYER = "console_player"
ADMIN = "console_admin"
PASS = "testpass1"

passed = []
failed = []
BASE = ""


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def req(method, path, data=None):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def jreq(method, path, data=None):
    status, raw = req(method, path, data)
    try:
        return status, json.loads(raw)
    except Exception:
        return status, {"_raw": raw[:300]}


def register(username, password):
    return jreq("POST", "/api/register",
                {"username": username, "password": password, "password2": password})[1]


def login(username, password):
    return jreq("POST", "/api/login", {"username": username, "password": password})[1]


def run_cmd(token, text):
    return jreq("POST", "/api/admin/command", {"token": token, "text": text})[1]


def wait_port(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            s.close()
            time.sleep(0.2)
    return False


def run():
    print("\n[1] 页面")
    status, page = req("GET", "/admin/command")
    check("GET /admin/command 返回 200", status == 200, status)
    check("页面含命令清单占位", "__OO_COMMANDS__" in page)
    check("页面含输入框", 'id="cmd"' in page)
    check("页面含权限拦截块", 'id="denied"' in page)
    check("页面含快捷面板", 'class="quick"' in page)

    print("\n[2] 权限：玩家 vs 管理员")
    tok_p = login(PLAYER, PASS).get("token") or ""
    tok_a = login(ADMIN, PASS).get("token") or ""
    check("两个账号都能登录", bool(tok_p) and bool(tok_a))

    st, r = jreq("POST", "/api/admin/command", {"token": "bogus", "text": "/help"})
    check("无效 token 返回 401", st == 401, st)

    r = run_cmd(tok_p, "/help")
    check("玩家能执行 /help", r.get("ok") is True, r.get("msg"))
    r = run_cmd(tok_a, "/help")
    check("管理员能执行 /help", r.get("ok") is True, r.get("msg"))
    text = "\n".join(r.get("lines") or [])
    check("帮助里有 /give", "/give" in text, text[:120])
    check("帮助里标注未实现", "未实现" in text)

    print("\n[3] 玩家权限边界")
    r = run_cmd(tok_p, "/account %s" % ADMIN)
    check("玩家不能查别人账号", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_p, "/account @")
    check("玩家能查自己账号", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, "/account %s perm 2" % PLAYER)
    check("玩家不能给自己提权", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_p, "/give @ 1001 1")
    check("玩家不能执行 GM 命令", r.get("ok") is False, r.get("lines"))

    print("\n[4] 基础命令")
    r = run_cmd(tok_a, "/who")
    check("/who 成功", r.get("ok") is True, r.get("msg"))
    who_text = "\n".join(r.get("lines") or [])
    check("/who 列出在线玩家", ADMIN in who_text, who_text[:120])

    r = run_cmd(tok_a, "/server")
    check("/server 成功", r.get("ok") is True, r.get("msg"))
    sv_text = "\n".join(r.get("lines") or [])
    check("/server 显示物品索引条数", "1866" in sv_text, sv_text[:200])
    check("/server 显示角色索引条数", "105" in sv_text, sv_text[:200])

    r = run_cmd(tok_a, "/clear")
    check("/clear 返回清屏标记", (r.get("data") or {}).get("clear") is True, r.get("data"))

    print("\n[5] 搜索命令")
    r = run_cmd(tok_a, "/search 长命锁")
    check("/search 成功", r.get("ok") is True, r.get("msg"))
    lines = "\n".join(r.get("lines") or [])
    check("搜到物品 1001", "1001" in lines, lines[:200])
    check("标注角色种类", "可玩" in lines or "变体" in lines, lines[:200])
    check("游戏内输出是单行", "\n" not in (r.get("chat") or ""), r.get("chat"))
    check("游戏内输出带分页提示", "/search" in (r.get("chat") or ""), r.get("chat"))

    r = run_cmd(tok_a, "/search 长命锁 2")
    check("/search 翻页成功", r.get("ok") is True, r.get("msg"))
    check("翻页内容不同", r.get("chat") != run_cmd(tok_a, "/search 长命锁").get("chat"),
          r.get("chat"))

    r = run_cmd(tok_a, "/search 绝对不存在的关键字xyz")
    check("搜不到返回失败", r.get("ok") is False, r.get("lines"))

    r = run_cmd(tok_a, "/search 长命锁 --allow-unavailables")
    check("--allow-unavailables 被识别且不报错", r.get("ok") is True, r.get("lines"))
    check("开关不影响搜索结果条数",
          (r.get("data") or {}).get("total") == (run_cmd(tok_a, "/search 长命锁").get("data") or {}).get("total"),
          r.get("data"))

    print("\n[6] 账号命令与提权")
    r = run_cmd(tok_a, "/account %s" % PLAYER)
    check("管理员能查别人账号", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_a, "/account %s perm 1" % PLAYER)
    check("管理员能给玩家提 GM", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, "/give @ 1001 1")
    check("提权后能执行 GM 命令（但功能未实现）",
          "尚未实现" in "\n".join(r.get("lines") or []), r.get("lines"))
    r = run_cmd(tok_a, "/account %s perm 2" % PLAYER)
    check("管理员能提到管理员", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, "/account %s" % ADMIN)
    check("升为管理员后能查别人", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_a, "/account %s perm 9" % PLAYER)
    check("非法权限值被拒", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, "/account %s perm 0" % PLAYER)
    check("能降回玩家", r.get("ok") is True, r.get("lines"))

    print("\n[7] 错误处理")
    r = run_cmd(tok_a, "/badcmd")
    check("未知命令报错", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, "help")
    check("缺 / 前缀报错", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, "/give")
    check("缺参数报错", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, "/give @")
    check("缺物品参数报错", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, "/account 不存在的账号")
    check("找不到账号报错", r.get("ok") is False, r.get("lines"))

    print("\n[8] 游戏内指令通道（ticket 认证）")
    lr = jreq("POST", "/api/launcher/login", {"username": PLAYER, "password": PASS})[1]
    check("启动器登录成功", lr.get("code") == 1, lr.get("msg"))
    data = lr.get("data") or {}
    ticket = data.get("ticket") or ""
    check("拿到票据", len(ticket) > 50, len(ticket))
    check("响应里带 web_base", bool(data.get("web_base")), sorted(data.keys()))

    st, r = jreq("POST", "/api/chat/command", {"ticket": "bogus", "text": "/who"})
    check("无效票据返回 401", st == 401, st)

    r = jreq("POST", "/api/chat/command", {"ticket": ticket, "text": "/help"})[1]
    check("凭票据能执行 /help", r.get("ok") is True, r.get("lines"))
    check("游戏内输出是单行", "\n" not in (r.get("chat") or ""), r.get("chat"))

    r = jreq("POST", "/api/chat/command", {"ticket": ticket, "text": "/server"})[1]
    check("凭票据能执行 /server", r.get("ok") is True, r.get("lines"))
    check("/server 的 chat 单行非空", bool(r.get("chat")), r.get("chat"))

    r = jreq("POST", "/api/chat/command", {"ticket": ticket, "text": "/give @ 1001 1"})[1]
    check("玩家用票据也不能执行 GM 命令", r.get("ok") is False, r.get("lines"))


def main():
    global BASE
    port = _free_port()
    BASE = "http://127.0.0.1:%d" % port
    tmpdir = tempfile.mkdtemp(prefix="oo_admin_")
    db_path = os.path.join(tmpdir, "test.db")

    env = dict(os.environ)
    env["OO_DB_PATH"] = db_path
    env["PYTHONIOENCODING"] = "utf-8"

    print("root =", ROOT)
    print("port =", port)
    print("db   =", db_path)

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "web.app", "--port", str(port), "--host", "127.0.0.1"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_port(port):
            print("FAIL: 服务没能在 25 秒内起来")
            return 2

        register(PLAYER, PASS)
        register(ADMIN, PASS)

        # 直接在库里把 ADMIN 提成管理员（模拟运维操作）
        os.environ["OO_DB_PATH"] = db_path
        sys.path.insert(0, ROOT)
        from shared import db as sdb
        sdb.init()
        sdb.set_perm_level(ADMIN, 2)
        sdb.set_perm_level(PLAYER, 0)

        run()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
