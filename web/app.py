#!/usr/bin/env python3
"""web.app —— OpenOrange 账号门户

独立启动:  python -m web.app
监听:      0.0.0.0:8399

一个账号，网页与启动器共用：
  1. 注册      账号名 + 密码（账号名 4~20 位字母/数字/下划线）
  2. 登录      换取网页会话 token
  3. 账号中心  账号信息 / 修改密码 / 删除账号
  4. 启动器     POST /api/launcher/login -> 官方同构的 {code,msg,data}
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import commands as cmds  # noqa: E402
from shared import db, models  # noqa: E402
from shared.log_tee import get_logger  # noqa: E402

_log = get_logger(__name__)

HOST = "0.0.0.0"
PORT = 8399

# ---------------------------------------------------------------- 服务器名
# 启动器启动时会向本服务查询，用于窗口标题与状态栏。
# 优先级：环境变量 OO_SERVER_NAME > server_name.txt > 默认值。
DEFAULT_SERVER_NAME = "OpenOrange"

def _server_name():
    env = (os.environ.get("OO_SERVER_NAME") or "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(os.path.dirname(here), "server_name.txt"),
        os.path.join(here, "server_name.txt"),
    ):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except Exception:
            pass
    return DEFAULT_SERVER_NAME

def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "-"

# ---------------------------------------------------------------- 登录限速
# 失败计数与判定在 web/ratelimit.py（P0-E3 抽出），这里只做名字兼容。
from web.ratelimit import (  # noqa: E402
    blocked as _login_blocked,
    note_fail as _note_login_fail,
    note_ok as _note_login_ok,
    rate_allow as _rate_allow,
)

# ---------------------------------------------------------------- 静态资源
# 样式 / 脚本 / 页面片段都在 web/static 与 web/templates 下（P0-E3 抽出），
# 由 web/assets.py 统一加载；这里只做名字兼容，保持既有调用点不变。
from web.assets import (  # noqa: E402
    ADMIN_BODY,
    ADMIN_SCRIPT,
    BODY,
    BRAND,
    OVERLAY,
    SCRIPT,
    STATIC_DIR as _STATIC_DIR,
    STATIC_TYPES as _STATIC_TYPES,
)

def _page(body):
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<title>OpenOrange 账号中心</title>'
        '<link rel="stylesheet" href="/static/app.css"></head><body>'
        '<div class="shell">' + BRAND + "<main class=\"content\">" + body + "</main></div>"
        + OVERLAY
        + '<div id="toast"></div>'
        "<script>" + SCRIPT + "</script></body></html>"
    )

def render_index():
    return _page(BODY)

# ---------------------------------------------------------------- 管理控制台

def render_admin():
    """管理控制台页面。命令清单内嵌进页面，省一次请求。"""
    info = []
    for c in cmds.COMMANDS.values():
        info.append({
            "name": c.name,
            "args": c.args,
            "desc": c.desc,
            "implemented": c.implemented,
            "group": c.group,
        })
    info.sort(key=lambda x: (x["group"], x["name"]))
    boot = "window.__OO_COMMANDS__ = " + json.dumps(info, ensure_ascii=False) + ";\n"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<title>归橙 · 管理控制台</title>'
        '<link rel="stylesheet" href="/static/admin.css"></head><body>'
        + ADMIN_BODY
        + "<script>" + boot + ADMIN_SCRIPT + "</script></body></html>"
    )

class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", status)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, name):
        """下发 web/static 下的静态文件。

        三重校验（审计 P2-7：只查 "/" 和 "\\" 不够，Windows 下
        os.path.join(dir, "C:app.css") 会变成 C:app.css）：
          1. 只认白名单后缀；
          2. basename(name) 必须等于 name（挡掉任何目录成分和盘符）；
          3. realpath 必须仍在 static 目录下。
        """
        if (not name or os.path.basename(name) != name or ":" in name
                or os.path.splitext(name)[1].lower() not in _STATIC_TYPES):
            return self._send("not found", "text/plain; charset=utf-8", 404)
        root = os.path.realpath(_STATIC_DIR)
        fp = os.path.realpath(os.path.join(root, name))
        if not fp.startswith(root + os.sep) or not os.path.isfile(fp):
            return self._send("not found", "text/plain; charset=utf-8", 404)
        with open(fp, "rb") as f:
            self._send(f.read(), _STATIC_TYPES[os.path.splitext(name)[1].lower()])

    # ---------------- GET ----------------

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._send(render_index())
            return

        if path in ("/admin/command", "/admin"):
            # 管理控制台页面本身不含数据，权限在前端拿到 /api/me 后再判断；
            # 真正的命令执行走 /api/admin/command，服务端会再校验一次权限。
            self._send(render_admin())
            return

        if path == "/api/stats":
            st = db.stats()
            st.pop("db_path", None)
            self._json(st)
            return

        if path == "/api/server/info":
            # 启动器启动时调用：拉取服务器显示名等公开信息。
            # 故意不鉴权 —— 只暴露展示用的名字，不含任何账号数据。
            st = db.stats()
            self._json({
                "code": 1,
                "msg": "Success",
                "data": {
                    "server_name": _server_name(),
                    "channel_id": "internal_pc",
                    "platform": "windows",
                    "accounts": st.get("accounts", 0),
                },
            })
            return

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        self._send("not found", "text/plain; charset=utf-8", 404)

    # ---------------- POST ----------------

    def do_POST(self):
        path = self.path.split("?")[0]
        req = self._body()

        if path == "/api/register":
            username = str(req.get("username") or "").strip()
            pw = str(req.get("password") or "")
            pw2 = req.get("password2")
            if pw2 is not None and pw != str(pw2):
                self._json({"ok": False, "msg": "两次输入的密码不一致"})
                return
            ok, msg, acc = models.register(username, pw)
            _log.info("[web] register %s username=%s -> %s" % (msg, username, ok))
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/login":
            username = str(req.get("username") or "").strip()
            if _login_blocked(username):
                self._json({"ok": False, "msg": "失败次数过多，请 10 分钟后再试"})
                return
            ok, msg, token = models.web_login(username, req.get("password"))
            if ok:
                _note_login_ok(username)
            else:
                _note_login_fail(username)
            _log.info("[web] login %s username=%s -> %s" % (msg, username, ok))
            self._json({"ok": ok, "msg": msg, "token": token or "", "username": username})
            return

        if path == "/api/me":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "登录已失效，请重新登录"}, 401)
                return
            prof = models.account_profile(acc)
            prof["created_text"] = _fmt_ts(prof["created"])
            prof["last_login_text"] = _fmt_ts(prof["last_login"])
            self._json({"ok": True, "account": prof})
            return

        if path == "/api/chat/command":
            # 游戏内聊天框指令入口。
            # 认证用启动器签发、客户端手里已有的 ticket（存在 tickets 表里，带过期时间），
            # 不需要额外的会话 token。
            t = models.check_ticket(str(req.get("ticket") or ""))
            if not t:
                self._json({"ok": False, "chat": "✗ 登录票据无效", "lines": ["登录票据无效"]}, 401)
                return
            username = t["username"]
            # ★ 雷6：按账号限流（票据在会话内复用，不能做成一次性）
            if not _rate_allow("chat:" + username):
                print("[chatcmd] rate limited username=%s" % username,
                      flush=True)
                self._json({"ok": False, "chat": "✗ 指令太频繁，请稍候",
                            "lines": ["指令太频繁，请稍候再用"]})
                return
            perm = db.get_perm_level(username)
            ctx = cmds.Context(username, perm, source="chat", server_name=_server_name())
            text = str(req.get("text") or "")
            res = cmds.execute(text, ctx)
            _log.info("[chatcmd] %s(perm=%d) > %s -> %s" % (username, perm, text, res.ok))
            self._json({
                "ok": res.ok,
                "chat": res.chat,
                "lines": res.lines,
                "data": res.data,
            })
            return

        if path == "/api/admin/command":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "登录已失效，请重新登录", "lines": ["登录已失效，请重新登录"]}, 401)
                return
            perm = int(acc.get("perm_level") or 0)
            ctx = cmds.Context(acc["username"], perm, source="web", server_name=_server_name())
            text = str(req.get("text") or "")
            res = cmds.execute(text, ctx)
            _log.info("[admin] %s(perm=%d) > %s -> %s" % (acc["username"], perm, text, res.ok))
            self._json({
                "ok": res.ok,
                "lines": res.lines,
                "chat": res.chat,
                "data": res.data,
                "actor": ctx.actor,
                "perm": perm,
            })
            return

        if path == "/api/launcher/login":
            # 启动器专用登录接口。
            # 返回与官方 login 响应同构的 {code, msg, data} —— 客户端脚本
            # deliver() 只认 result.code == 1，随后取 result.data.account_id / ticket
            # （见 modules/sdk/fpxaccount.lua 第 12-13 行）。
            username = str(req.get("username") or "").strip()
            password = str(req.get("password") or "")
            if _login_blocked(username):
                _log.info("[launcher] login BLOCKED username=%s" % username)
                self._json({"code": 0, "msg": "失败次数过多，请稍后再试", "data": {}})
                return

            # 只校验密码，不建网页会话 —— 启动器不需要会话，
            # 建了反而会把网页端正在登录的那一端挤掉。
            ok, msg, acc = models.verify_credentials(username, password)
            if not ok:
                _note_login_fail(username)
                _log.info("[launcher] login REJECTED username=%s: %s" % (username, msg))
                self._json({"code": 0, "msg": msg, "data": {}})
                return

            ret, err = models.login(username)
            if err:
                _note_login_fail(username)
                _log.warning("[launcher] login FAILED username=%s: %s" % (username, err))
                self._json({"code": 0, "msg": err, "data": {}})
                return

            _note_login_ok(username)
            # 游戏内指令通道要回连本服务，把地址一并下发。
            host = self.headers.get("Host") or ""
            if host:
                ret["web_base"] = "http://" + host
            print("[launcher] login OK username=%s account_id=%s fp_uid=%s"
                  % (username, ret.get("account_id"), ret.get("fp_uid")), flush=True)
            self._json({"code": 1, "msg": "Success", "data": ret})
            return

        if path == "/api/logout":
            models.web_logout(req.get("token") or "")
            self._json({"ok": True, "msg": "已退出"})
            return

        if path == "/api/change_password":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "未登录"}, 401)
                return
            ok, msg = models.change_password(
                acc["username"],
                req.get("old_password"),
                req.get("password"),
                req.get("password2"),
                keep_token=req.get("token") or "",
            )
            _log.info("[web] change_password username=%s -> %s" % (acc["username"], ok))
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/delete_account":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "未登录"}, 401)
                return
            ok, msg = models.remove_account(acc["username"], req.get("password"))
            _log.info("[web] delete_account username=%s -> %s" % (acc["username"], ok))
            self._json({"ok": ok, "msg": msg})
            return

        self._json({"ok": False, "msg": "not found"}, 404)

def serve(host=None, port=None):
    db.init()
    host = host or HOST
    port = port or PORT
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    _log.info("[web] http://%s:%d" % (host, port))
    _log.info("[web] server_name = %s" % _server_name())
    return httpd

def main():
    """命令行启动。

    用法:
        python -m web.app                       # 默认 0.0.0.0:8399
        python -m web.app --port 9000           # 指定端口
        python -m web.app --port 8399 --host 127.0.0.1
    """
    # ★ 统一日志（与 game 通道同款；装失败不挡启动）
    try:
        from shared.log_tee import install as _log_install
        _log_dir = os.environ.get("OO_LOG_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "work", "logs")
        _log_install("web", _log_dir)
    except Exception as _e:
        _log.warning("[log] 统一日志安装失败，退回控制台模式: %s" % _e)

    argv = sys.argv[1:]
    host = None
    port = None
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
    httpd = serve(host=host, port=port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log.info("[web] shutting down")

if __name__ == "__main__":
    main()
