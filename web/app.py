#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web.app —— OpenOrange 账号门户

独立启动:  python -m web.app
监听:      0.0.0.0:8399

一个账号，网页与启动器共用：
  1. 注册      手机号 + 密码（手机号当账号名，不校验真实性）
  2. 登录      换取网页会话 token
  3. 账号中心  账号信息 / 修改密码 / 删除账号
  4. 启动器     POST /api/launcher/login -> 官方同构的 {code,msg,data}
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db, models  # noqa: E402

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
# 同一账号 10 分钟内失败 5 次即暂时拒绝，防止在线爆破。
# 只放内存，进程重启即清零 —— 私服场景够用，不必落库。
LOGIN_WINDOW = 600
LOGIN_MAX_FAIL = 5
_login_fail_lock = threading.Lock()
_login_fails = {}


def _login_blocked(mobile):
    now = time.time()
    with _login_fail_lock:
        arr = [t for t in _login_fails.get(mobile, []) if now - t < LOGIN_WINDOW]
        _login_fails[mobile] = arr
        return len(arr) >= LOGIN_MAX_FAIL


def _note_login_fail(mobile):
    with _login_fail_lock:
        _login_fails.setdefault(mobile, []).append(time.time())


def _note_login_ok(mobile):
    with _login_fail_lock:
        _login_fails.pop(mobile, None)


# ---------------------------------------------------------------- 页面
CSS = """
:root{
  --bg:#0f1115; --card:#171a20; --line:#262b34; --text:#e7e9ee;
  --muted:#8b929e; --accent:#f0a23c; --ok:#46c47a; --err:#f16b6b;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{
  margin:0;min-height:100vh;color:var(--text);
  background:radial-gradient(1200px 620px at 50% -12%,#1e242f 0%,var(--bg) 58%);
  font-family:"Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
  display:flex;flex-direction:column;align-items:center;padding:30px 16px 70px;
}
header{width:100%;max-width:720px;display:flex;justify-content:space-between;
       align-items:center;margin-bottom:26px;gap:16px}
.brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);
     box-shadow:0 0 10px var(--ok);flex:none}
.nav{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--muted)}
.nav b{color:var(--text);font-weight:600}
main{width:100%;max-width:720px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
      padding:22px 22px 24px;margin-bottom:16px}
.card h2{font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.5px;margin:0 0 16px}
.tabs{display:flex;gap:6px;background:#101318;border:1px solid var(--line);
      border-radius:10px;padding:4px;margin-bottom:20px}
.tab{flex:1;padding:9px 0;border:0;border-radius:7px;background:transparent;
     color:var(--muted);font-size:14px;cursor:pointer;font-family:inherit;transition:.15s}
.tab.active{background:#242a34;color:var(--text)}
label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
input{width:100%;padding:11px 13px;background:#0d1014;border:1px solid #2c323d;
      border-radius:9px;color:var(--text);font-size:14px;font-family:inherit;transition:.15s}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(240,162,60,.12)}
button{font-family:inherit;cursor:pointer}
.primary{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;
         background:var(--accent);color:#12151a;font-size:15px;font-weight:600}
.primary:hover{background:#ffb554}
.ghost{padding:7px 14px;background:#222831;border:1px solid var(--line);
       border-radius:8px;color:#c6ccd6;font-size:13px}
.ghost:hover{background:#2b323d}
.hint{margin:16px 0 0;font-size:12.5px;color:var(--muted);line-height:1.75}
.hint b{color:#b9c2d0;font-weight:600}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
    gap:16px 22px;margin:0}
.kv dt{font-size:12px;color:var(--muted);margin-bottom:5px}
.kv dd{margin:0;font-size:14px;word-break:break-all}
.mono{font-family:Consolas,ui-monospace,monospace;font-size:13px;color:#a9b4c4}
.danger{border-color:#3a2426}
.danger h2{color:#f08585}
.dangerBtn{width:100%;margin-top:20px;padding:11px;border:1px solid #5a2a2c;
           border-radius:9px;background:#2a1719;color:#f08585;font-size:14px}
.dangerBtn:hover{background:#381d20}
#toast{position:fixed;left:50%;bottom:34px;transform:translateX(-50%) translateY(16px);
       padding:11px 20px;border-radius:10px;font-size:13.5px;opacity:0;
       pointer-events:none;transition:.22s;max-width:88vw;text-align:center}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.ok{background:#17321f;color:#63d98c;border:1px solid #245c33}
#toast.err{background:#331a1c;color:#ff8b8b;border:1px solid #5c2a2c}
"""

BODY = """
<section id="authView">
  <div class="card">
    <div class="tabs">
      <button class="tab active" data-tab="login" onclick="switchTab('login')">登录</button>
      <button class="tab" data-tab="reg" onclick="switchTab('reg')">注册</button>
    </div>

    <form id="loginForm" onsubmit="return doLogin(event)">
      <label>账号</label>
      <input id="loginMobile" placeholder="11 位手机号" maxlength="11" autocomplete="username">
      <label>密码</label>
      <input id="loginPass" type="password" placeholder="请输入密码" autocomplete="current-password">
      <button class="primary" type="submit">登录</button>
    </form>

    <form id="regForm" hidden onsubmit="return doRegister(event)">
      <label>账号（11 位手机号，只当账号名用，不会真的发短信）</label>
      <input id="regMobile" placeholder="例如 13800138000" maxlength="11" autocomplete="username">
      <label>密码（至少 6 位）</label>
      <input id="regPass" type="password" placeholder="设置密码" autocomplete="new-password">
      <label>确认密码</label>
      <input id="regPass2" type="password" placeholder="再输入一次" autocomplete="new-password">
      <button class="primary" type="submit">注册并登录</button>
    </form>

    <p class="hint">同一个账号，<b>网页</b>和<b>启动器</b>都能登录。注册完直接去启动器登录即可。</p>
  </div>
</section>

<section id="acctView" hidden>
  <div class="card">
    <h2>账号信息</h2>
    <dl class="kv">
      <div><dt>账号</dt><dd id="iMobile"></dd></div>
      <div><dt>账号 ID</dt><dd id="iAccount" class="mono"></dd></div>
      <div><dt>玩家 UID</dt><dd id="iUid" class="mono"></dd></div>
      <div><dt>注册时间</dt><dd id="iCreated"></dd></div>
      <div><dt>最后登录</dt><dd id="iLast"></dd></div>
      <div><dt>登录次数</dt><dd id="iCount"></dd></div>
    </dl>
  </div>

  <div class="card">
    <h2>修改密码</h2>
    <label>当前密码</label>
    <input id="pwOld" type="password" autocomplete="current-password">
    <label>新密码（至少 6 位）</label>
    <input id="pwNew" type="password" autocomplete="new-password">
    <label>确认新密码</label>
    <input id="pwNew2" type="password" autocomplete="new-password">
    <button class="primary" onclick="doChangePass()">保存新密码</button>
  </div>

  <div class="card danger">
    <h2>删除账号</h2>
    <p class="hint">删除后账号、登录票据与网页会话会一并清除，<b>无法恢复</b>。</p>
    <label>输入密码确认</label>
    <input id="delPass" type="password" autocomplete="current-password">
    <button class="dangerBtn" onclick="doDelete()">删除账号</button>
  </div>
</section>
"""

SCRIPT = """
var TOKEN = localStorage.getItem("oo_token") || "";

function $(id){ return document.getElementById(id); }

function toast(t, ok){
  var el = $("toast");
  el.textContent = t;
  el.className = "show " + (ok ? "ok" : "err");
  clearTimeout(toast._t);
  toast._t = setTimeout(function(){ el.className = ""; }, 2600);
}

function post(path, data, cb){
  var x = new XMLHttpRequest();
  x.open("POST", path, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var r;
    try { r = JSON.parse(x.responseText); } catch(e){ r = null; }
    cb(r || {ok:false, msg:"服务器返回异常"});
  };
  x.send(JSON.stringify(data || {}));
}

function get(path, cb){
  var x = new XMLHttpRequest();
  x.open("GET", path, true);
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var r;
    try { r = JSON.parse(x.responseText); } catch(e){ r = null; }
    cb(r);
  };
  x.send();
}

function switchTab(which){
  $("loginForm").hidden = which !== "login";
  $("regForm").hidden   = which !== "reg";
  var tabs = document.querySelectorAll(".tab");
  for(var i = 0; i < tabs.length; i++){
    var on = tabs[i].getAttribute("data-tab") === which;
    tabs[i].className = "tab" + (on ? " active" : "");
  }
}

function doLogin(e){
  if(e) e.preventDefault();
  var m = $("loginMobile").value.trim();
  if(!m){ toast("请输入账号", false); return false; }
  post("/api/login", {mobile:m, password:$("loginPass").value}, function(r){
    if(!r.ok){ toast(r.msg, false); return; }
    TOKEN = r.token;
    localStorage.setItem("oo_token", TOKEN);
    toast("登录成功", true);
    refresh();
  });
  return false;
}

function doRegister(e){
  if(e) e.preventDefault();
  var m = $("regMobile").value.trim();
  var p = $("regPass").value;
  if(p !== $("regPass2").value){ toast("两次输入的密码不一致", false); return false; }
  post("/api/register", {mobile:m, password:p, password2:$("regPass2").value}, function(r){
    if(!r.ok){ toast(r.msg, false); return; }
    post("/api/login", {mobile:m, password:p}, function(r2){
      if(!r2.ok){ toast("注册成功，请手动登录", true); switchTab("login"); return; }
      TOKEN = r2.token;
      localStorage.setItem("oo_token", TOKEN);
      toast("注册成功，已自动登录", true);
      refresh();
    });
  });
  return false;
}

function doLogout(){
  post("/api/logout", {token:TOKEN}, function(){
    TOKEN = "";
    localStorage.removeItem("oo_token");
    refresh();
    toast("已退出登录", true);
  });
}

function doChangePass(){
  post("/api/change_password", {
    token: TOKEN,
    old_password: $("pwOld").value,
    password: $("pwNew").value,
    password2: $("pwNew2").value
  }, function(r){
    toast(r.msg, r.ok);
    if(r.ok){ $("pwOld").value = ""; $("pwNew").value = ""; $("pwNew2").value = ""; }
  });
}

function doDelete(){
  if(!confirm("确定删除该账号？此操作不可恢复。")) return;
  post("/api/delete_account", {token:TOKEN, password:$("delPass").value}, function(r){
    toast(r.msg, r.ok);
    if(r.ok){
      TOKEN = "";
      localStorage.removeItem("oo_token");
      refresh();
    }
  });
}

function showAuth(){
  $("authView").hidden = false;
  $("acctView").hidden = true;
  $("nav").hidden = true;
}

function showAcct(a){
  $("authView").hidden = true;
  $("acctView").hidden = false;
  $("nav").hidden = false;
  $("whoMob").textContent = a.mobile;
  $("iMobile").textContent = a.mobile;
  $("iAccount").textContent = a.account_id;
  $("iUid").textContent = a.uid;
  $("iCreated").textContent = a.created_text;
  $("iLast").textContent = a.last_login_text;
  $("iCount").textContent = a.login_count;
}

function refresh(){
  if(!TOKEN){ showAuth(); return; }
  post("/api/me", {token:TOKEN}, function(r){
    if(!r || !r.ok){
      TOKEN = "";
      localStorage.removeItem("oo_token");
      showAuth();
      return;
    }
    showAcct(r.account);
  });
}

get("/api/server/info", function(r){
  if(r && r.data && r.data.server_name){ $("srvName").textContent = r.data.server_name; }
});

refresh();
"""


def _page(body, script):
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>OpenOrange 账号中心</title><style>' + CSS + "</style></head><body>"
        "<header>"
        '<div class="brand"><span class="dot"></span><span id="srvName">OpenOrange</span></div>'
        '<div class="nav" id="nav" hidden><span>已登录 <b id="whoMob"></b></span>'
        '<button class="ghost" onclick="doLogout()">退出</button></div>'
        "</header>"
        "<main>" + body + "</main>"
        '<div id="toast"></div>'
        "<script>" + script + "</script></body></html>"
    )


def render_index():
    return _page(BODY, SCRIPT)


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
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._send(render_index())
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

        self._send("not found", "text/plain; charset=utf-8", 404)

    # ---------------- POST ----------------

    def do_POST(self):
        path = self.path.split("?")[0]
        req = self._body()

        if path == "/api/register":
            mobile = str(req.get("mobile") or "").strip()
            pw = str(req.get("password") or "")
            pw2 = req.get("password2")
            if pw2 is not None and pw != str(pw2):
                self._json({"ok": False, "msg": "两次输入的密码不一致"})
                return
            ok, msg, acc = models.register(mobile, pw)
            print("[web] register %s mobile=%s -> %s" % (msg, mobile, ok), flush=True)
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/login":
            mobile = str(req.get("mobile") or "").strip()
            if _login_blocked(mobile):
                self._json({"ok": False, "msg": "失败次数过多，请 10 分钟后再试"})
                return
            ok, msg, token = models.web_login(mobile, req.get("password"))
            if ok:
                _note_login_ok(mobile)
            else:
                _note_login_fail(mobile)
            print("[web] login %s mobile=%s -> %s" % (msg, mobile, ok), flush=True)
            self._json({"ok": ok, "msg": msg, "token": token or "", "mobile": mobile})
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

        if path == "/api/launcher/login":
            # 启动器专用登录接口。
            # 返回与官方 login 响应同构的 {code, msg, data} —— 客户端脚本
            # deliver() 只认 result.code == 1，随后取 result.data.account_id / ticket
            # （见 modules/sdk/fpxaccount.lua 第 12-13 行）。
            mobile = str(req.get("mobile") or "").strip()
            password = str(req.get("password") or "")
            if _login_blocked(mobile):
                print("[launcher] login BLOCKED mobile=%s" % mobile, flush=True)
                self._json({"code": 0, "msg": "失败次数过多，请稍后再试", "data": {}})
                return

            # 只校验密码，不建网页会话 —— 启动器不需要会话，
            # 建了反而会把网页端正在登录的那一端挤掉。
            ok, msg, acc = models.verify_credentials(mobile, password)
            if not ok:
                _note_login_fail(mobile)
                print("[launcher] login REJECTED mobile=%s: %s" % (mobile, msg), flush=True)
                self._json({"code": 0, "msg": msg, "data": {}})
                return

            ret, err = models.login(mobile)
            if err:
                _note_login_fail(mobile)
                print("[launcher] login FAILED mobile=%s: %s" % (mobile, err), flush=True)
                self._json({"code": 0, "msg": err, "data": {}})
                return

            _note_login_ok(mobile)
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
            ok, msg = models.change_password(
                acc["mobile"],
                req.get("old_password"),
                req.get("password"),
                req.get("password2"),
                keep_token=req.get("token") or "",
            )
            print("[web] change_password mobile=%s -> %s" % (acc["mobile"], ok), flush=True)
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/delete_account":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "未登录"}, 401)
                return
            ok, msg = models.remove_account(acc["mobile"], req.get("password"))
            print("[web] delete_account mobile=%s -> %s" % (acc["mobile"], ok), flush=True)
            self._json({"ok": ok, "msg": msg})
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
        python -m web.app                       # 默认 0.0.0.0:8399
        python -m web.app --port 9000           # 指定端口
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
