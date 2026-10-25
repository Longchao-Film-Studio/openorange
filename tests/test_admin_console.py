#!/usr/bin/env python3
"""管理控制台端到端测试。

用法:
    python tests/test_admin_console.py

会临时起一个 web.app（随机端口 + 临时数据库），跑完自动清理。
"""
import contextlib
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

    print("\n[1b] 公告页面（2026-10-26 新增）")
    status, page = req("GET", "/admin/announce")
    check("GET /admin/announce 返回 200", status == 200, status)
    check("含类型选择", 'id="kind"' in page)
    check("含目标输入", 'id="uid"' in page)
    check("含文案输入", 'id="text"' in page)
    check("含历史面板", 'id="history"' in page)
    check("含权限拦截块", 'id="denied"' in page)
    _st, _console = req("GET", "/admin/command")
    check("控制台有公告入口", "/admin/announce" in _console)

    print("\n[2] 权限：玩家 vs 管理员")
    tok_p = login(PLAYER, PASS).get("token") or ""
    tok_a = login(ADMIN, PASS).get("token") or ""
    check("两个账号都能登录", bool(tok_p) and bool(tok_a))

    st, r = jreq("POST", "/api/admin/command", {"token": "bogus", "text": "/help"})
    check("无效 token 返回 401", st == 401, st)

    st, _ = jreq("POST", "/api/admin/announce",
                 {"token": "bogus", "kind": "banner", "text": "x"})
    check("公告接口：无效 token 返回 401", st == 401, st)
    r = run_cmd(tok_p, "/help")
    check("玩家能执行 /help", r.get("ok") is True, r.get("msg"))
    r = run_cmd(tok_a, "/help")
    check("管理员能执行 /help", r.get("ok") is True, r.get("msg"))
    text = "\n".join(r.get("lines") or [])
    check("帮助里有 /give", "/give" in text, text[:120])
    # ★ 诚实化：未接后端的命令在帮助里明确标「(未实现)」，
    #   不再假装全部已实现。
    check("帮助里未实现命令有标注", "未实现" in text, text[:300])
    check("帮助里 give 已实现（无标注）", "/give " in text and "give" in text, text[:300])
    check("帮助里有 /announce", "/announce" in text, text[:300])

    print("\n[3] 玩家权限边界")
    r = run_cmd(tok_p, f"/account {ADMIN}")
    check("玩家不能查别人账号", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_p, "/account @")
    check("玩家能查自己账号", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, f"/account {PLAYER} perm 2")
    check("玩家不能给自己提权", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_p, "/give @ 1001 1")
    check("玩家不能执行 GM 命令", r.get("ok") is False, r.get("lines"))
    st, r = jreq("POST", "/api/admin/announce",
                 {"token": tok_p, "kind": "banner", "text": "x"})
    check("公告接口：玩家 403", st == 403, st)
    r = run_cmd(tok_p, "/announce 玩家不该能发")
    check("玩家不能执行 /announce", r.get("ok") is False, r.get("lines"))

    print("\n[4] 基础命令")
    r = run_cmd(tok_a, "/who")
    check("/who 成功", r.get("ok") is True, r.get("msg"))
    who_text = "\n".join(r.get("lines") or [])
    # /who 现在只统计**真正进了游戏**的连接（game_online 表，由 game_server 维护）。
    # 以前读的是 sessions（网页会话），启动器每登录一次留一条、过期又长，
    # 结果同一个账号能列出 8 条。测试环境没有游戏服在跑，所以应该是 0 人。
    check("/who 不把网页会话当在线", ADMIN not in who_text and PLAYER not in who_text,
          who_text[:120])
    check("/who 显示 0 人", "在线 0" in (r.get("chat") or "") or "0 人" in who_text,
          (r.get("chat"), who_text[:120]))

    r = run_cmd(tok_a, "/server")
    check("/server 成功", r.get("ok") is True, r.get("msg"))
    sv_text = "\n".join(r.get("lines") or [])
    check("/server 显示物品索引条数", "2728" in sv_text, sv_text[:200])
    check("/server 显示角色索引条数", "193" in sv_text, sv_text[:200])

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
    r = run_cmd(tok_a, f"/account {PLAYER}")
    check("管理员能查别人账号", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_a, f"/account {PLAYER} perm 1")
    check("管理员能给玩家提 GM", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, "/give @ 1001 1")
    _gv = "\n".join(r.get("lines") or [])
    check("提权后能执行 GM 命令（真接线）", r.get("ok") is True, _gv)
    check("GM 命令真发放了物品", "长命锁" in _gv and "已发放" in _gv, _gv)
    check("GM 命令不再标『未生效』", "未生效" not in _gv, _gv)
    check("GM 命令标记 applied=True", (r.get("data") or {}).get("applied") is True,
          (r.get("data") or {}))
    r = run_cmd(tok_a, f"/account {PLAYER} perm 2")
    check("管理员能提到管理员", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_p, f"/account {ADMIN}")
    check("升为管理员后能查别人", r.get("ok") is True, r.get("lines"))
    r = run_cmd(tok_a, f"/account {PLAYER} perm 9")
    check("非法权限值被拒", r.get("ok") is False, r.get("lines"))
    r = run_cmd(tok_a, f"/account {PLAYER} perm 0")
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
    BASE = f'http://127.0.0.1:{port}'
    tmpdir = tempfile.mkdtemp(prefix="oo_admin_")
    db_path = os.path.join(tmpdir, "test.db")
    state_dir = os.path.join(tmpdir, "player_state")

    # ★ 2026-10-23：/give /lv /money 已真接线到游戏服管理接口。
    #   为了让「GM 命令能真发放」有真实后端可打，测试进程里也起一个
    #   admin_api（像 test_gm_commands 那样），web 子进程连它。
    admin_port = _free_port()
    admin_token = "oo-test-admin-token"
    env = dict(os.environ)
    env["OO_DB_PATH"] = db_path
    env["OO_STATE_DIR"] = state_dir
    env["OO_ADMIN_PORT"] = str(admin_port)
    env["OO_ADMIN_TOKEN"] = admin_token
    env["OO_ADMIN_URL"] = f'http://127.0.0.1:{admin_port}'
    env["PYTHONIOENCODING"] = "utf-8"

    os.environ["OO_DB_PATH"] = db_path
    os.environ["OO_STATE_DIR"] = state_dir
    os.environ["OO_ADMIN_PORT"] = str(admin_port)
    os.environ["OO_ADMIN_TOKEN"] = admin_token
    os.environ["OO_ADMIN_URL"] = f'http://127.0.0.1:{admin_port}'

    # 在测试进程内起 admin_api（线程），web 子进程通过 HTTP 连它
    from game_server import admin_api as _adm
    srv, _th = _adm.start(host="127.0.0.1", port=admin_port)

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
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            srv.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
