#!/usr/bin/env python3
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

# 自由用户名；另有一个纯数字账号用来验证「老式手机号账号」仍然兼容
U1 = "test_user_01"
U2 = "testuser02"
U_LEGACY = "19900000001"
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


def reg(username, password, password2=None):
    if password2 is None:
        password2 = password
    return jreq("POST", "/api/register",
                {"username": username, "password": password, "password2": password2})[1]


def login(username, password):
    return jreq("POST", "/api/login", {"username": username, "password": password})[1]


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
    check("输入框用浮动标签（无裸 label 堆叠）",
          'class="field"' in page and 'placeholder=" "' in page)
    check("改密 / 删号走弹窗", 'id="overlay"' in page and 'id="modalPw"' in page)
    check("含左侧品牌区", 'class="brand-side"' in page)

    print("\n[2] 账号名校验（自由用户名）")
    r = reg("ab", P1)
    check("太短被拒（2 位）", r.get("ok") is False, r)
    r = reg("a" * 21, P1)
    check("太长被拒（21 位）", r.get("ok") is False, r)
    r = reg("has space", P1)
    check("含空格被拒", r.get("ok") is False, r)
    r = reg("中文账号", P1)
    check("非 ASCII 被拒", r.get("ok") is False, r)
    r = reg("dash-name", P1)
    check("含短横线被拒", r.get("ok") is False, r)
    r = reg(U1, P1, "mismatch")
    check("两次密码不一致被拒", r.get("ok") is False, r)
    r = reg(U1, "123")
    check("密码过短被拒", r.get("ok") is False, r)
    r = reg(U1, P1)
    check("自由用户名注册成功", r.get("ok") is True, r)
    r = reg(U1, P1)
    check("重复注册被拒", r.get("ok") is False, r)
    r = reg(U_LEGACY, P1)
    check("老式纯数字账号仍可注册（兼容）", r.get("ok") is True, r)

    print("\n[3] 登录与会话")
    r = login(U1, "wrongpass")
    check("错误密码被拒", r.get("ok") is False, r)
    r = login(U1, P1)
    check("正确密码登录成功", r.get("ok") is True, r)
    token = r.get("token") or ""
    check("拿到会话 token", len(token) > 20, len(token))
    token2 = login(U1, P1).get("token") or ""
    check("支持多端同时登录", bool(token2) and token2 != token)
    check("旧会话未被新登录挤掉", me(token)[1].get("ok") is True)

    print("\n[4] 账号信息")
    status, r = me(token)
    acc = r.get("account") or {}
    check("/api/me 成功", r.get("ok") is True, r)
    check("回显用户名", acc.get("username") == U1, acc.get("username"))
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
    check("旧密码已失效", login(U1, P1).get("ok") is False)
    check("新密码可登录", login(U1, P2).get("ok") is True)

    print("\n[6] 启动器登录契约")
    r = jreq("POST", "/api/launcher/login", {"username": U1, "password": "wrongpass"})[1]
    check("错误密码 -> code=0", r.get("code") == 0, r)
    r = jreq("POST", "/api/launcher/login", {"username": U1, "password": P2})[1]
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
    r = jreq("POST", "/api/launcher/login", {"username": U_LEGACY, "password": P1})[1]
    check("老式纯数字账号也能启动器登录", r.get("code") == 1, r)

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
    check("删除后无法登录", login(U1, P2).get("ok") is False)

    print("\n[9] 登录限速")
    reg(U2, P1)
    tok = login(U2, P1).get("token") or ""
    for i in range(5):
        login(U2, "nope%d" % i)
    r = login(U2, P1)
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
