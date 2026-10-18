#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号门户端到端测试。

用法:
    python tests/test_account_portal.py

会临时起一个 web.app（随机端口 + 临时数据库），跑完自动清理，
不会动到 shared/data.db。
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

M1 = "19900000001"
M2 = "19900000002"
P1 = "testpass1"
P2 = "testpass2"

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


def reg(mobile, password, password2=None):
    if password2 is None:
        password2 = password
    return jreq("POST", "/api/register",
                {"mobile": mobile, "password": password, "password2": password2})[1]


def login(mobile, password):
    return jreq("POST", "/api/login", {"mobile": mobile, "password": password})[1]


def me(token):
    return jreq("POST", "/api/me", {"token": token})


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
    status, page = req("GET", "/")
    check("GET / 返回 200", status == 200, status)
    check("含 登录/注册 两个页签",
          'data-tab="login"' in page and 'data-tab="reg"' in page)
    check("不含已下线的验证码界面", "验证码" not in page)
    check("含账号中心字段", "账号 ID" in page and "玩家 UID" in page)

    print("\n[2] 注册校验")
    r = reg(M1, P1, "mismatch")
    check("两次密码不一致被拒", r.get("ok") is False, r)
    r = reg("12345", P1)
    check("非法账号被拒", r.get("ok") is False, r)
    r = reg(M1, "123")
    check("密码过短被拒", r.get("ok") is False, r)
    r = reg(M1, P1)
    check("正常注册成功", r.get("ok") is True, r)
    r = reg(M1, P1)
    check("重复注册被拒", r.get("ok") is False, r)

    print("\n[3] 登录与会话")
    r = login(M1, "wrongpass")
    check("错误密码被拒", r.get("ok") is False, r)
    r = login(M1, P1)
    check("正确密码登录成功", r.get("ok") is True, r)
    token = r.get("token") or ""
    check("拿到会话 token", len(token) > 20, len(token))
    token2 = login(M1, P1).get("token") or ""
    check("支持多端同时登录", bool(token2) and token2 != token)
    check("旧会话未被新登录挤掉", me(token)[1].get("ok") is True)

    print("\n[4] 账号信息")
    status, r = me(token)
    acc = r.get("account") or {}
    check("/api/me 成功", r.get("ok") is True, r)
    check("含 account_id", str(acc.get("account_id", "")).startswith("oo_"), acc.get("account_id"))
    check("含玩家 UID", isinstance(acc.get("uid"), int) and acc["uid"] > 0, acc.get("uid"))
    check("含注册时间", bool(acc.get("created_text")), acc.get("created_text"))
    check("含登录次数", acc.get("login_count", 0) >= 1, acc.get("login_count"))
    check("不泄露密码哈希", "pass_hash" not in json.dumps(acc), acc)
    status, _ = me("bogus")
    check("无效 token 返回 401", status == 401, status)

    print("\n[5] 修改密码")
    r = jreq("POST", "/api/change_password",
             {"token": token, "old_password": "bad", "password": P2, "password2": P2})[1]
    check("当前密码错 -> 拒绝", r.get("ok") is False, r)
    r = jreq("POST", "/api/change_password",
             {"token": token, "old_password": P1, "password": P2, "password2": "x"})[1]
    check("新密码两次不一致 -> 拒绝", r.get("ok") is False, r)
    r = jreq("POST", "/api/change_password",
             {"token": token, "old_password": P1, "password": P2, "password2": P2})[1]
    check("正常改密成功", r.get("ok") is True, r)
    check("当前会话改密后仍有效", me(token)[1].get("ok") is True)
    check("其它端会话被踢掉", me(token2)[1].get("ok") is not True)
    check("旧密码已失效", login(M1, P1).get("ok") is False)
    check("新密码可登录", login(M1, P2).get("ok") is True)

    print("\n[6] 启动器登录契约")
    r = jreq("POST", "/api/launcher/login", {"mobile": M1, "password": "wrongpass"})[1]
    check("错误密码 -> code=0", r.get("code") == 0, r)
    r = jreq("POST", "/api/launcher/login", {"mobile": M1, "password": P2})[1]
    check("登录成功 -> code=1", r.get("code") == 1, r)
    data = r.get("data") or {}
    check("含 account_id", bool(data.get("account_id")), data.get("account_id"))
    check("含 ticket", len(str(data.get("ticket", ""))) > 50)
    check("fp_uid 是整数", isinstance(data.get("fp_uid"), int), type(data.get("fp_uid")))
    need = set(["account_create_ts", "account_id", "bind_infos", "channel_uid", "current_fpid",
                "delete_time_left", "fp_device_id", "fp_uid", "fpid", "fpid_list", "is_formal",
                "is_new", "session_key", "ticket"])
    check("官方 14 字段齐全", need.issubset(set(data.keys())), sorted(data.keys()))
    check("启动器登录不挤掉网页会话", me(token)[1].get("ok") is True)

    print("\n[7] 公开接口")
    r = jreq("GET", "/api/server/info")[1]
    check("server/info -> code=1", r.get("code") == 1, r)
    check("含 server_name", bool((r.get("data") or {}).get("server_name")), r)
    r = jreq("GET", "/api/stats")[1]
    check("stats 无 codes 字段", "codes" not in r, r)
    status, _ = req("GET", "/api/codes")
    check("/api/codes 已下线", status == 404, status)

    print("\n[8] 删除账号")
    r = jreq("POST", "/api/delete_account", {"token": token, "password": "wrong"})[1]
    check("密码错 -> 拒绝删除", r.get("ok") is False, r)
    r = jreq("POST", "/api/delete_account", {"token": token, "password": P2})[1]
    check("正确密码 -> 删除成功", r.get("ok") is True, r)
    status, _ = me(token)
    check("删除后会话失效", status == 401, status)
    check("删除后无法登录", login(M1, P2).get("ok") is False)

    print("\n[9] 登录限速")
    reg(M2, P1)
    tok = login(M2, P1).get("token") or ""
    for i in range(5):
        login(M2, "nope%d" % i)
    r = login(M2, P1)
    check("连续失败后正确密码也被暂时拒绝", r.get("ok") is False, r)
    check("提示限速文案", "过多" in str(r.get("msg", "")), r.get("msg"))
    r = jreq("POST", "/api/delete_account", {"token": tok, "password": P1})[1]
    check("限速不影响已有会话的账号管理", r.get("ok") is True, r)


def main():
    global BASE
    port = _free_port()
    BASE = "http://127.0.0.1:%d" % port
    tmpdir = tempfile.mkdtemp(prefix="oo_test_")
    env = dict(os.environ)
    env["OO_DB_PATH"] = os.path.join(tmpdir, "test.db")
    env["PYTHONIOENCODING"] = "utf-8"

    print("root =", ROOT)
    print("port =", port)
    print("db   =", env["OO_DB_PATH"])

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "web.app", "--port", str(port), "--host", "127.0.0.1"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_port(port):
            print("FAIL: 服务没能在 25 秒内起来")
            return 2
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
