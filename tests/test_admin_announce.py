#!/usr/bin/env python
"""公告接口 + /announce 指令 端到端测试。

链路（和线上一致）：
    网页 / 指令 --HTTP--> 游戏服 admin_api (POST /announce) --> announce --> online.broadcast/send_to

所以本测试在**同一进程**里起真实 admin_api，并在 online 表里挂假连接，
发一条公告后直接检查假连接收到的字节 —— 不 mock 关键路径。

用法: python tests/test_admin_announce.py
"""
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PORT = 33198
_TOKEN = "announcetok"
os.environ["OO_ADMIN_PORT"] = str(_PORT)
os.environ["OO_ADMIN_TOKEN"] = _TOKEN
os.environ["OO_ADMIN_URL"] = f"http://127.0.0.1:{_PORT}"
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_annapi_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_annapi_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import admin_api, announce  # noqa: E402
from game_server import ms as msmod
from game_server.transport import online  # noqa: E402
from shared import commands as cmds  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


class FakeConn:
    def __init__(self):
        self.payloads = []

    def sendall(self, data):
        self.payloads.append(bytes(data))


def http(method, path, payload=None, tok=_TOKEN):
    url = f"http://127.0.0.1:{_PORT}{path}"
    data = None
    headers = {}
    if tok is not None:
        headers["X-OO-Token"] = tok
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"_raw": "?"}


def wait_port(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        try:
            s.settimeout(0.3)
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.05)
        finally:
            s.close()
    return False


def run():
    online.reset_for_tests()
    announce.reset_for_tests()

    srv, _th = admin_api.start(port=_PORT)
    if not wait_port(_PORT):
        print("admin_api 未起来")
        return 1

    try:
        print("\n[1] 鉴权：没有 token 一律拒绝")
        st, _ = http("POST", "/announce", {"text": "x"}, tok=None)
        check("无 token -> 401/403", st in (401, 403), st)
        st, _ = http("POST", "/announce", {"text": "x"}, tok="wrong-token")
        check("错 token -> 401/403", st in (401, 403), st)

        print("\n[2] 全服广播：两条已登录连接都收到，未登录跳过")
        a, b, guest = FakeConn(), FakeConn(), FakeConn()
        online.register(1, a); online.set_uid(1, "u1")
        online.register(2, b); online.set_uid(2, "u2")
        online.register(3, guest)
        st, r = http("POST", "/announce", {"kind": "banner", "text": "全服维护通知"})
        check("HTTP 200 + ok", st == 200 and r.get("ok") is True, (st, r))
        check("targets=2", r.get("targets") == 2, r)
        check("两条连接都收到帧", a.payloads and b.payloads)
        check("未登录连接没收到", guest.payloads == [])
        if a.payloads:
            _t, m, _ri, p = msmod.parse_entity_response(a.payloads[0])
            check("帧内容 = sendWorldBanner + 文案",
                  m == "sendWorldBanner" and p == ["全服维护通知"], (m, p))

        print("\n[3] 定向发送：只给指定账号")
        a.payloads.clear(); b.payloads.clear()
        st, r = http("POST", "/announce", {"kind": "toast", "text": "只给你", "uid": "u2"})
        check("HTTP 200 + ok", st == 200 and r.get("ok") is True, (st, r))
        check("只发了 1 条", r.get("targets") == 1, r)
        check("u1 没收到", a.payloads == [], len(a.payloads))
        check("u2 收到", len(b.payloads) == 1, len(b.payloads))

        print("\n[4] 参数错误：400 且说明原因")
        st, r = http("POST", "/announce", {"kind": "banner", "text": ""})
        check("空文案 -> 400", st == 400 and r.get("ok") is False, (st, r))
        st, r = http("POST", "/announce", {"kind": "不存在", "text": "x"})
        check("未知类型 -> 400 并列出可选", st == 400 and "banner" in (r.get("error") or ""), (st, r))
        st, r = http("POST", "/announce", {"kind": "banner", "text": "长" * 81})
        check("跑马灯超长 -> 400", st == 400 and "80" in (r.get("error") or ""), (st, r))

        print("\n[5] GET /announce：类型清单 + 历史")
        st, r = http("GET", "/announce")
        check("HTTP 200", st == 200, st)
        check("三类清单", len(r.get("kinds") or []) == 3, r.get("kinds"))
        check("历史有记录（含失败项）", len(r.get("history") or []) >= 4,
              len(r.get("history") or []))

        print("\n[6] /announce 指令：GM 权限可用，走同一后端")
        a.payloads.clear()
        old_url = cmds.ADMIN_URL
        cmds.ADMIN_URL = f"http://127.0.0.1:{_PORT}"
        try:
            ctx = cmds.Context("gm_user", cmds.PERM_GM, source="web")
            res = cmds.execute("/announce 指令发的公告", ctx)
            check("指令 ok", res.ok is True, res.lines)
            check("连接收到帧", len(a.payloads) == 1, len(a.payloads))
            res2 = cmds.execute("/announce toast 指令Toast", ctx)
            check("指定 toast 类型", res2.ok is True and "toast" in str(res2.data), res2.data)
            ctx_p = cmds.Context("player_user", cmds.PERM_PLAYER, source="web")
            res3 = cmds.execute("/announce 玩家不该能发", ctx_p)
            check("玩家权限不足", res3.ok is False and "权限" in "".join(res3.lines), res3.lines)
            res4 = cmds.execute("/announce", ctx)
            check("缺参数给用法", res4.ok is False and "用法" in "".join(res4.lines), res4.lines)
        finally:
            cmds.ADMIN_URL = old_url

        print("\n[7] 没人在线时：HTTP 400 + 诚实原因")
        online.reset_for_tests()
        st, r = http("POST", "/announce", {"kind": "banner", "text": "空场"})
        check("400 且说明没人在线", st == 400 and "没有在线玩家" in (r.get("error") or ""),
              (st, r))
    finally:
        srv.shutdown()
        srv.server_close()
        online.reset_for_tests()
        announce.reset_for_tests()

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
