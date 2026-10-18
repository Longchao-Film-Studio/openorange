#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web.app —— 玩家门户页

独立启动:  python -m web.app
监听:      0.0.0.0:8322

功能:
  1. 注册  (手机号 + 密码; 手机号当 UID, 不验证真实性)
  2. 登录  (登录后才会下发验证码)
  3. 验证码展示 (只读, 5 秒自动刷新)
  4. 账号管理 (改密码 / 删除账号)

流程: 玩家先在网页注册 -> 网页登录 -> 再进游戏点"获取验证码"
"""
import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db, models  # noqa: E402

HOST = "0.0.0.0"
PORT = 8322

# ---------------------------------------------------------------- 服务器名
# 服务器显示名：客户端（启动器）启动时向本服务查询，用于窗口标题与状态栏。
# 优先级：环境变量 OO_SERVER_NAME > 文件 server_name.txt > 默认值。
# 改名字不需要重启启动器，重启 web 服务即可（或改环境变量后重启）。
DEFAULT_SERVER_NAME = "OpenOrange"


def _server_name():
    """读取服务器显示名。"""
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
        return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return "-"


CSS = """
*{box-sizing:border-box}
body{background:#14161a;color:#e6e6e6;font-family:"Microsoft YaHei",system-ui,sans-serif;margin:0;padding:24px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:22px;color:#ffb84d;margin:0 0 4px}
.sub{color:#8a8f98;font-size:13px;margin-bottom:20px}
.card{background:#1b1e24;border:1px solid #2a2e36;border-radius:10px;padding:20px;margin-bottom:18px}
.card h2{font-size:15px;color:#9aa0a8;font-weight:normal;margin:0 0 14px}
label{display:block;font-size:13px;color:#9aa0a8;margin:10px 0 5px}
input{width:100%;max-width:320px;padding:9px 12px;background:#101216;border:1px solid #333844;
      border-radius:6px;color:#e6e6e6;font-size:14px;font-family:inherit}
input:focus{outline:none;border-color:#ffb84d}
button{margin-top:14px;padding:9px 22px;background:#ffb84d;border:0;border-radius:6px;
       color:#14161a;font-size:14px;font-weight:bold;cursor:pointer;font-family:inherit}
button:hover{background:#ffc76b}
button.ghost{background:#2a2e36;color:#c9ced6;font-weight:normal}
button.ghost:hover{background:#343946}
.msg{margin-top:12px;padding:9px 12px;border-radius:6px;font-size:13px}
.ok{background:#16351f;color:#4dd47f;border:1px solid #245c33}
.err{background:#3a1b1b;color:#ff7b7b;border:1px solid #632a2a}
table{border-collapse:collapse;width:100%;background:#101216;border-radius:8px;overflow:hidden}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid #23272f;font-size:14px}
th{background:#22262e;color:#9aa0a8;font-weight:normal;font-size:13px}
td.code{font-size:24px;font-weight:bold;color:#4dd47f;letter-spacing:4px;font-family:Consolas,monospace}
.used{color:#555;text-decoration:line-through}
.empty{padding:28px;color:#8a8f98;text-align:center;font-size:14px}
.stat{margin-top:14px;color:#6f757e;font-size:12px;text-align:center}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.who{font-size:13px;color:#8a8f98}
.who b{color:#4dd47f}
"""


def _page(body, script=""):
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>OpenOrange 私服</title><style>" + CSS + "</style></head><body>"
            "<div class=\"wrap\">" + body + "</div>"
            "<script>" + script + "</script></body></html>")


def render_index():
    body = """
<h1>OpenOrange 私服门户</h1>
<div class="sub">先注册账号，再登录，然后才能在游戏内获取验证码</div>

<div class="card">
  <div id="topbar" class="bar" style="display:none">
    <div class="who">已登录: <b id="whoMob"></b></div>
    <button class="ghost" onclick="logout()">退出登录</button>
  </div>
  <div id="authBox">
    <h2>注册 / 登录</h2>
    <label>手机号（当作账号 UID，不验证真实性）</label>
    <input id="mobile" placeholder="例如 13800138000" maxlength="11" autocomplete="off">
    <label>密码（至少 6 位）</label>
    <input id="password" type="password" placeholder="请输入密码" autocomplete="off">
    <div>
      <button onclick="doRegister()">注册</button>
      <button class="ghost" onclick="doLogin()" style="margin-left:8px">登录</button>
    </div>
    <div id="msg"></div>
  </div>
</div>

<div class="card">
  <h2>验证码（登录后出现；在游戏内点"获取验证码"才会生成）</h2>
  <div id="codeArea"><div class="empty">请先注册并登录</div></div>
</div>

<div class="card">
  <h2>账号管理</h2>
  <label>新密码</label>
  <input id="newpass" type="password" placeholder="改密码（至少 6 位）">
  <button class="ghost" onclick="changePass()">修改密码</button>
  <button class="ghost" onclick="delAccount()" style="color:#ff7b7b">删除账号</button>
  <div id="msg2"></div>
</div>

<div class="stat" id="stat"></div>
"""
    script = """
var TOKEN = localStorage.getItem("oo_token") || "";
var MOB   = localStorage.getItem("oo_mobile") || "";

function say(t, ok){
  var el = document.getElementById("msg");
  el.className = "msg " + (ok ? "ok" : "err");
  el.textContent = t;
  setTimeout(function(){ el.className=""; el.textContent=""; }, 4000);
}
function say2(t, ok){
  var el = document.getElementById("msg2");
  el.className = "msg " + (ok ? "ok" : "err");
  el.textContent = t;
  setTimeout(function(){ el.className=""; el.textContent=""; }, 4000);
}

function post(path, data, cb){
  var x = new XMLHttpRequest();
  x.open("POST", path, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.onreadystatechange = function(){
    if(x.readyState === 4){
      try { cb(JSON.parse(x.responseText)); }
      catch(e){ cb({ok:false, msg:"服务器错误"}); }
    }
  };
  x.send(JSON.stringify(data));
}

function doRegister(){
  post("/api/register", {mobile:v("mobile"), password:v("password")}, function(r){
    say(r.msg, r.ok);
    if(r.ok){ setTimeout(doLogin, 600); }
  });
}
function doLogin(){
  post("/api/login", {mobile:v("mobile"), password:v("password")}, function(r){
    say(r.msg, r.ok);
    if(r.ok){
      TOKEN = r.token; MOB = r.mobile;
      localStorage.setItem("oo_token", TOKEN);
      localStorage.setItem("oo_mobile", MOB);
      refreshUI();
    }
  });
}
function logout(){
  post("/api/logout", {token:TOKEN}, function(){
    TOKEN=""; MOB="";
    localStorage.removeItem("oo_token");
    localStorage.removeItem("oo_mobile");
    refreshUI();
  });
}
function changePass(){
  post("/api/change_password", {token:TOKEN, password:v("newpass")}, function(r){
    say2(r.msg, r.ok);
  });
}
function delAccount(){
  if(!confirm("确定删除该账号？此操作不可恢复")) return;
  post("/api/delete_account", {token:TOKEN}, function(r){
    say2(r.msg, r.ok);
    if(r.ok) logout();
  });
}
function v(id){ return document.getElementById(id).value.trim(); }

function refreshUI(){
  var authBox = document.getElementById("authBox");
  var topbar  = document.getElementById("topbar");
  if(TOKEN && MOB){
    authBox.style.display = "none";
    topbar.style.display  = "flex";
    document.getElementById("whoMob").textContent = MOB;
    loadCodes();
  } else {
    authBox.style.display = "block";
    topbar.style.display  = "none";
    document.getElementById("codeArea").innerHTML =
      '<div class="empty">请先注册并登录</div>';
    document.getElementById("stat").textContent = "";
  }
}

function loadCodes(){
  if(!TOKEN) return;
  var x = new XMLHttpRequest();
  x.open("GET", "/api/codes?token=" + encodeURIComponent(TOKEN), true);
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var r;
    try { r = JSON.parse(x.responseText); } catch(e){ return; }
    var area = document.getElementById("codeArea");
    if(!r.codes || !r.codes.length){
      area.innerHTML = '<div class="empty">暂无验证码，请到游戏内点"获取验证码"</div>';
      return;
    }
    var h = '<table><tr><th>手机号</th><th>验证码</th><th>状态</th><th>签发</th><th>过期</th></tr>';
    for(var i=0;i<r.codes.length;i++){
      var c = r.codes[i];
      h += "<tr" + (c.used ? ' class="used"' : "") + ">"
         + "<td>" + c.mobile + "</td>"
         + '<td class="code">' + c.code + "</td>"
         + "<td>" + (c.used ? "已使用" : "待使用") + "</td>"
         + "<td>" + c.issued + "</td><td>" + c.expire + "</td></tr>";
    }
    h += "</table>";
    area.innerHTML = h;
    if(r.stat) document.getElementById("stat").textContent = r.stat;
  };
  x.send();
}

refreshUI();
setInterval(loadCodes, 5000);
"""
    return _page(body, script)


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

    # ---------------- GET ----------------

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = parse_qs(qs)

        if path in ("/", "/index.html"):
            self._send(render_index())
            return

        if path == "/api/codes":
            token = (q.get("token") or [""])[0]
            acc = models.whoami(token)
            if not acc:
                self._json({"ok": False, "msg": "not logged in", "codes": []}, 401)
                return
            codes = [c for c in db.list_codes(50) if c["mobile"] == acc["mobile"]][:20]
            st = db.stats()
            self._json({
                "ok": True,
                "codes": [{
                    "mobile": c["mobile"],
                    "code": c["code"],
                    "used": bool(c["used"]),
                    "issued": _fmt_ts(c["issued_ts"]),
                    "expire": _fmt_ts(c["expire_ts"]),
                } for c in codes],
                "stat": "账号 %d | 验证码 %d | 票据 %d | 在线会话 %d" % (
                    st["accounts"], st["codes"], st["tickets"], st["sessions"]),
            })
            return

        if path == "/api/stats":
            self._json(db.stats())
            return

        if path == "/api/server/info":
            # 启动器启动时调用：拉取服务器显示名等公开信息。
            # 故意不鉴权 —— 这里只暴露展示用的名字，不含任何账号数据。
            name = _server_name()
            st = db.stats()
            self._json({
                "code": 1,
                "msg": "Success",
                "data": {
                    "server_name": name,
                    "channel_id": "internal_pc",
                    "platform": "windows",
                    "accounts": st.get("accounts", 0),
                },
            })
            return

        self._send("not found", "text/plain; charset=utf-8", 404)

    # ---------------- POST ----------------

    def do_POST(self):
        path = self.path.split("?")[0]
        req = self._body()

        if path == "/api/register":
            ok, msg, acc = models.register(req.get("mobile"), req.get("password"))
            print("[web] register %s mobile=%s -> %s" % (msg, req.get("mobile"), ok), flush=True)
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/login":
            ok, msg, token = models.web_login(req.get("mobile"), req.get("password"))
            print("[web] login %s mobile=%s -> %s" % (msg, req.get("mobile"), ok), flush=True)
            self._json({"ok": ok, "msg": msg, "token": token or "", "mobile": req.get("mobile") or ""})
            return

        if path == "/api/launcher/login":
            # 启动器专用登录接口。
            # 返回与官方 login 响应同构的 {code, msg, data} —— 客户端脚本
            # deliver() 只认 result.code == 1，随后取 result.data.account_id / ticket
            # （见 modules/sdk/fpxaccount.lua 第 12-13 行）。
            mobile = str(req.get("mobile") or "").strip()
            password = str(req.get("password") or "")
            ok, msg, token = models.web_login(mobile, password)
            if not ok:
                print("[launcher] login REJECTED mobile=%s: %s" % (mobile, msg), flush=True)
                self._json({"code": 0, "msg": msg, "data": {}})
                return

            ret, err = models.login(mobile)
            if err:
                print("[launcher] login FAILED mobile=%s: %s" % (mobile, err), flush=True)
                self._json({"code": 0, "msg": err, "data": {}})
                return

            print("[launcher] login OK mobile=%s account_id=%s fp_uid=%s"
                  % (mobile, ret.get("account_id"), ret.get("fp_uid")), flush=True)
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
            pw = str(req.get("password") or "")
            if len(pw) < 6:
                self._json({"ok": False, "msg": "密码至少 6 位"})
                return
            db.set_password(acc["mobile"], pw)
            print("[web] change_password mobile=%s" % acc["mobile"], flush=True)
            self._json({"ok": True, "msg": "密码已修改"})
            return

        if path == "/api/delete_account":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "未登录"}, 401)
                return
            db.delete_account(acc["mobile"])
            print("[web] delete_account mobile=%s" % acc["mobile"], flush=True)
            self._json({"ok": True, "msg": "账号已删除"})
            return

        self._json({"ok": False, "msg": "not found"}, 404)


def serve(host=None, port=None):
    db.init()
    host = host or HOST
    port = port or PORT
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    print("[web] http://%s:%d" % (host, port), flush=True)
    print("[web] server_name = %s" % _server_name(), flush=True)
    return httpd


def main():
    """命令行启动。

    用法:
        python -m web.app                     # 默认 0.0.0.0:8322
        python -m web.app --port 8399         # 指定端口（启动器 ini 默认指向 8399）
        python -m web.app --port 8399 --host 127.0.0.1
    """
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
        print("[web] shutting down", flush=True)


if __name__ == "__main__":
    main()
