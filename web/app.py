#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import commands as cmds  # noqa: E402
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


def _login_blocked(username):
    now = time.time()
    with _login_fail_lock:
        arr = [t for t in _login_fails.get(username, []) if now - t < LOGIN_WINDOW]
        _login_fails[username] = arr
        return len(arr) >= LOGIN_MAX_FAIL


def _note_login_fail(username):
    with _login_fail_lock:
        _login_fails.setdefault(username, []).append(time.time())


def _note_login_ok(username):
    with _login_fail_lock:
        _login_fails.pop(username, None)


# ---------------------------------------------------------------- 样式
CSS = """
:root{
  --bg:#08090c; --text:#f4f5f7; --muted:#8b909c;
  --line:rgba(255,255,255,.08);
  --accent:#ff9f43; --accent2:#ff6a00;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
html,body{height:100%}
body{
  margin:0;background:var(--bg);color:var(--text);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
}
body::before{
  content:"";position:fixed;inset:0;pointer-events:none;
  background:
    radial-gradient(900px 600px at 80% -12%,rgba(255,106,0,.10),transparent 62%),
    radial-gradient(760px 520px at 6% 112%,rgba(59,130,246,.09),transparent 62%);
}
.shell{position:relative;display:flex;min-height:100vh}

/* ---------- 左侧品牌区 ---------- */
.brand-side{
  position:relative;flex:0 0 44%;max-width:600px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:space-between;
  padding:44px 52px;border-right:1px solid var(--line);
  background:linear-gradient(180deg,rgba(255,255,255,.022),transparent 42%);
}
.orb{position:absolute;border-radius:50%;filter:blur(80px);pointer-events:none}
.orb-a{width:460px;height:460px;top:-170px;left:-150px;background:rgba(255,106,0,.50);
       animation:drift 16s ease-in-out infinite}
.orb-b{width:380px;height:380px;bottom:-160px;right:-140px;background:rgba(59,130,246,.32);
       animation:drift 21s ease-in-out infinite reverse}
@keyframes drift{
  0%,100%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(26px,32px,0) scale(1.06)}
}
.brand-top{position:relative;display:flex;align-items:center;gap:12px;
           font-size:15px;font-weight:600;letter-spacing:.2px}
.mark{display:grid;place-items:center;width:32px;height:32px;border-radius:10px;
      background:linear-gradient(135deg,var(--accent),var(--accent2));
      color:#1a0f05;font-size:12px;font-weight:800;letter-spacing:.6px}
.brand-foot{position:relative;display:flex;align-items:center;gap:9px;
            font-size:13px;color:var(--muted)}
.pulse{width:8px;height:8px;border-radius:50%;background:#46c47a;flex:none;
       animation:pulse 2.4s ease-out infinite}
@keyframes pulse{
  0%{box-shadow:0 0 0 0 rgba(70,196,122,.55)}
  70%{box-shadow:0 0 0 9px rgba(70,196,122,0)}
  100%{box-shadow:0 0 0 0 rgba(70,196,122,0)}
}

/* ---------- 右侧内容区 ---------- */
.content{position:relative;flex:1;display:flex;align-items:center;
         justify-content:center;padding:48px 24px}

/* ---------- 登录 / 注册 ---------- */
.auth-card{
  width:100%;max-width:392px;padding:30px 28px 26px;border-radius:22px;
  background:rgba(255,255,255,.025);border:1px solid var(--line);
  backdrop-filter:blur(14px);box-shadow:0 26px 70px -32px rgba(0,0,0,.95);
}
.seg{position:relative;display:flex;padding:5px;margin-bottom:26px;border-radius:13px;
     background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06)}
.seg-pill{position:absolute;top:5px;left:5px;width:calc(50% - 5px);height:calc(100% - 10px);
          border-radius:9px;background:rgba(255,255,255,.10);
          box-shadow:0 2px 10px rgba(0,0,0,.3);
          transition:transform .28s cubic-bezier(.4,0,.2,1)}
.seg-pill.right{transform:translateX(100%)}
.seg-btn{position:relative;z-index:1;flex:1;height:36px;border:0;background:none;
         color:var(--muted);font:inherit;font-size:14px;cursor:pointer;transition:color .2s}
.seg-btn.active{color:var(--text);font-weight:600}

.field{position:relative;margin-bottom:14px}
.field input{
  width:100%;height:56px;padding:22px 15px 8px;border-radius:12px;
  background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.09);
  color:var(--text);font:inherit;font-size:14.5px;
  transition:border-color .18s,background .18s,box-shadow .18s;
}
.field input:hover{border-color:rgba(255,255,255,.16)}
.field input:focus{
  outline:none;border-color:rgba(255,159,67,.62);background:rgba(255,255,255,.055);
  box-shadow:0 0 0 4px rgba(255,106,0,.12);
}
.field label{
  position:absolute;left:15px;top:18px;font-size:14.5px;color:var(--muted);
  pointer-events:none;transform-origin:left top;
  transition:transform .16s cubic-bezier(.4,0,.2,1),color .16s;
}
.field input:focus + label,
.field input:not(:placeholder-shown) + label{
  transform:translateY(-10px) scale(.76);color:var(--accent);
}
.field input:not(:focus):not(:placeholder-shown) + label{color:var(--muted)}

.btn-primary{
  position:relative;width:100%;height:50px;margin-top:10px;border:0;border-radius:12px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#1a0f05;font:inherit;font-size:15px;font-weight:700;letter-spacing:.3px;
  cursor:pointer;transition:transform .16s,box-shadow .16s,opacity .16s;
  box-shadow:0 10px 26px -12px rgba(255,106,0,.8);
}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 14px 32px -12px rgba(255,106,0,.9)}
.btn-primary:active{transform:translateY(0) scale(.995)}
.btn-primary[disabled]{opacity:.6;cursor:default;transform:none;box-shadow:none}

.btn-ghost{
  height:36px;padding:0 16px;border-radius:10px;border:1px solid var(--line);
  background:rgba(255,255,255,.04);color:#c9ced8;font:inherit;font-size:13px;
  cursor:pointer;transition:.16s;flex:none;
}
.btn-ghost:hover{background:rgba(255,255,255,.08);color:var(--text)}

.btn-danger{
  height:44px;padding:0 20px;border-radius:11px;
  border:1px solid rgba(241,107,107,.35);background:rgba(241,107,107,.12);
  color:#ff9090;font:inherit;font-size:14px;font-weight:600;cursor:pointer;transition:.16s;
}
.btn-danger:hover{background:rgba(241,107,107,.2)}
.btn-danger[disabled]{opacity:.6;cursor:default}

.foot-hint{margin:18px 0 0;text-align:center;font-size:12px;color:#6c7280;line-height:1.6}

/* ---------- 账号中心 ---------- */
.acct{width:100%;max-width:600px}
.acct-head{display:flex;align-items:center;gap:15px;margin-bottom:26px}
.avatar{
  width:54px;height:54px;border-radius:17px;display:grid;place-items:center;flex:none;
  font-size:22px;font-weight:800;color:#fff;text-transform:uppercase;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  box-shadow:0 14px 30px -14px rgba(255,106,0,.95);
}
.acct-id{flex:1;min-width:0}
.acct-name{font-size:19px;font-weight:700;letter-spacing:-.2px;
           overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acct-sub{font-size:12.5px;color:var(--muted);margin-top:3px}

.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:14px}
.stat{display:flex;flex-direction:column;gap:7px;padding:16px 17px;border-radius:14px;
      background:rgba(255,255,255,.03);border:1px solid var(--line)}
.stat-k{font-size:12px;color:var(--muted)}
.stat-v{font-size:15px;font-weight:600;word-break:break-all}
.mono{font-family:Consolas,ui-monospace,monospace;font-size:13.5px;
      color:#b9c2d2;letter-spacing:.2px}

.rows{border-radius:16px;overflow:hidden;background:rgba(255,255,255,.03);
      border:1px solid var(--line)}
.row{display:flex;align-items:center;justify-content:space-between;gap:16px;width:100%;
     padding:17px 19px;border:0;border-bottom:1px solid var(--line);background:none;
     color:var(--text);font:inherit;text-align:left;cursor:pointer;transition:background .16s}
.row:last-child{border-bottom:0}
.row:hover{background:rgba(255,255,255,.045)}
.row-main{display:flex;flex-direction:column;gap:4px;min-width:0}
.row-t{font-size:14.5px;font-weight:600}
.row-t.danger{color:#ff9090}
.row-d{font-size:12.5px;color:var(--muted)}
.chev{color:#5a6069;font-size:20px;line-height:1;flex:none}

/* ---------- 弹窗 ---------- */
.overlay{
  position:fixed;inset:0;z-index:60;display:grid;place-items:center;padding:22px;
  background:rgba(4,5,8,.72);backdrop-filter:blur(8px);animation:fade .18s ease;
}
.modal{
  width:100%;max-width:380px;padding:24px;border-radius:20px;background:#12151b;
  border:1px solid rgba(255,255,255,.1);
  box-shadow:0 30px 80px -22px rgba(0,0,0,.95);
  animation:pop .22s cubic-bezier(.2,.9,.3,1.15);
}
.modal h3{margin:0 0 6px;font-size:17px;font-weight:700;letter-spacing:-.2px}
.modal-desc{margin:0 0 18px;font-size:13px;color:var(--muted);line-height:1.65}
.modal .field{margin-bottom:12px}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes pop{
  from{opacity:0;transform:translateY(10px) scale(.97)}
  to{opacity:1;transform:none}
}

/* ---------- 提示 ---------- */
#toast{
  position:fixed;left:50%;bottom:30px;z-index:80;transform:translate(-50%,14px);
  padding:11px 20px;border-radius:12px;font-size:13.5px;opacity:0;
  pointer-events:none;transition:.24s;max-width:88vw;text-align:center;
  backdrop-filter:blur(10px);
}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.ok{background:rgba(23,50,31,.95);color:#6ddc95;border:1px solid rgba(36,92,51,.9)}
#toast.err{background:rgba(51,26,28,.95);color:#ff9090;border:1px solid rgba(92,42,44,.9)}

@media (max-width:880px){
  .brand-side{display:none}
  .content{padding:34px 18px}
}
"""

BRAND = """
<aside class="brand-side">
  <div class="orb orb-a"></div>
  <div class="orb orb-b"></div>
  <div class="brand-top"><span class="mark">OO</span><span id="srvName">OpenOrange</span></div>
  <div class="brand-foot"><span class="pulse"></span><span>服务运行中</span></div>
</aside>
"""

BODY = """
<section id="authView" class="auth-card">
  <div class="seg">
    <div class="seg-pill" id="segPill"></div>
    <button class="seg-btn active" data-tab="login" onclick="switchTab('login')">登录</button>
    <button class="seg-btn" data-tab="reg" onclick="switchTab('reg')">注册</button>
  </div>

  <form id="loginForm" onsubmit="return doLogin(event)">
    <div class="field">
      <input id="loginUser" placeholder=" " autocomplete="username" spellcheck="false">
      <label for="loginUser">账号</label>
    </div>
    <div class="field">
      <input id="loginPass" type="password" placeholder=" " autocomplete="current-password">
      <label for="loginPass">密码</label>
    </div>
    <button class="btn-primary" id="loginBtn" type="submit">登录</button>
  </form>

  <form id="regForm" hidden onsubmit="return doRegister(event)">
    <div class="field">
      <input id="regUser" placeholder=" " autocomplete="username" spellcheck="false" maxlength="20">
      <label for="regUser">账号名</label>
    </div>
    <div class="field">
      <input id="regPass" type="password" placeholder=" " autocomplete="new-password">
      <label for="regPass">密码</label>
    </div>
    <div class="field">
      <input id="regPass2" type="password" placeholder=" " autocomplete="new-password">
      <label for="regPass2">确认密码</label>
    </div>
    <button class="btn-primary" id="regBtn" type="submit">创建账号</button>
  </form>

  <p class="foot-hint" id="hintLogin">账号名 4~20 位，字母 / 数字 / 下划线</p>
  <p class="foot-hint" id="hintReg" hidden>密码至少 6 位 · 注册后自动登录</p>
</section>

<section id="acctView" class="acct" hidden>
  <div class="acct-head">
    <div class="avatar" id="avatar">O</div>
    <div class="acct-id">
      <div class="acct-name" id="acctName"></div>
      <div class="acct-sub">已登录</div>
    </div>
    <button class="btn-ghost" onclick="doLogout()">退出</button>
  </div>

  <div class="stats">
    <div class="stat"><span class="stat-k">账号 ID</span><span class="stat-v mono" id="iAccount"></span></div>
    <div class="stat"><span class="stat-k">玩家 UID</span><span class="stat-v mono" id="iUid"></span></div>
    <div class="stat"><span class="stat-k">注册时间</span><span class="stat-v" id="iCreated"></span></div>
    <div class="stat"><span class="stat-k">登录次数</span><span class="stat-v" id="iCount"></span></div>
  </div>

  <div class="rows">
    <button class="row" onclick="openModal('pw')">
      <span class="row-main">
        <span class="row-t">修改密码</span>
        <span class="row-d">定期更换密码，账号更安全</span>
      </span>
      <span class="chev">&#8250;</span>
    </button>
    <button class="row" onclick="openModal('del')">
      <span class="row-main">
        <span class="row-t danger">删除账号</span>
        <span class="row-d">账号与登录票据一并清除，不可恢复</span>
      </span>
      <span class="chev">&#8250;</span>
    </button>
  </div>
</section>
"""

OVERLAY = """
<div class="overlay" id="overlay" hidden onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modalPw" hidden>
    <h3>修改密码</h3>
    <p class="modal-desc">需要先验证当前密码。改完后其它设备上的登录会被踢下线。</p>
    <div class="field">
      <input id="pwOld" type="password" placeholder=" " autocomplete="current-password">
      <label for="pwOld">当前密码</label>
    </div>
    <div class="field">
      <input id="pwNew" type="password" placeholder=" " autocomplete="new-password">
      <label for="pwNew">新密码</label>
    </div>
    <div class="field">
      <input id="pwNew2" type="password" placeholder=" " autocomplete="new-password">
      <label for="pwNew2">确认新密码</label>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal()">取消</button>
      <button class="btn-primary" id="pwBtn" style="width:auto;margin-top:0;padding:0 22px;height:36px;font-size:13.5px" onclick="doChangePass()">保存</button>
    </div>
  </div>

  <div class="modal" id="modalDel" hidden>
    <h3>删除账号</h3>
    <p class="modal-desc">账号、登录票据与网页会话会一并清除，<b>无法恢复</b>。</p>
    <div class="field">
      <input id="delPass" type="password" placeholder=" " autocomplete="current-password">
      <label for="delPass">输入密码确认</label>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeModal()">取消</button>
      <button class="btn-danger" id="delBtn" onclick="doDelete()">删除账号</button>
    </div>
  </div>
</div>
"""

SCRIPT = """
var TOKEN = localStorage.getItem("oo_token") || "";

function $(id){ return document.getElementById(id); }

function toast(msg, ok){
  var el = $("toast");
  el.textContent = msg;
  el.className = "show " + (ok ? "ok" : "err");
  clearTimeout(toast._t);
  toast._t = setTimeout(function(){ el.className = ""; }, 2800);
}

function post(path, data, cb){
  var x = new XMLHttpRequest();
  x.open("POST", path, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var r = null;
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
    var r = null;
    try { r = JSON.parse(x.responseText); } catch(e){ r = null; }
    cb(r);
  };
  x.send();
}

function busy(btn, on, text){
  if(!btn) return;
  if(on){
    if(btn.dataset.label === undefined) btn.dataset.label = btn.textContent;
    btn.textContent = text || "处理中…";
    btn.disabled = true;
  } else {
    if(btn.dataset.label !== undefined) btn.textContent = btn.dataset.label;
    btn.disabled = false;
  }
}

function switchTab(which){
  var isLogin = which === "login";
  $("loginForm").hidden = !isLogin;
  $("regForm").hidden = isLogin;
  $("hintLogin").hidden = !isLogin;
  $("hintReg").hidden = isLogin;
  $("segPill").className = "seg-pill" + (isLogin ? "" : " right");
  var btns = document.querySelectorAll(".seg-btn");
  for(var i = 0; i < btns.length; i++){
    var on = btns[i].getAttribute("data-tab") === which;
    btns[i].className = "seg-btn" + (on ? " active" : "");
  }
}

function doLogin(e){
  if(e) e.preventDefault();
  var u = $("loginUser").value.trim();
  var p = $("loginPass").value;
  if(!u){ toast("请输入账号", false); return false; }
  if(!p){ toast("请输入密码", false); return false; }
  var btn = $("loginBtn");
  busy(btn, true, "登录中…");
  post("/api/login", {username:u, password:p}, function(r){
    busy(btn, false);
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
  var u = $("regUser").value.trim();
  var p = $("regPass").value;
  var p2 = $("regPass2").value;
  if(!/^[A-Za-z0-9_]{4,20}$/.test(u)){ toast("账号名需 4~20 位字母、数字或下划线", false); return false; }
  if(p.length < 6){ toast("密码至少 6 位", false); return false; }
  if(p !== p2){ toast("两次输入的密码不一致", false); return false; }
  var btn = $("regBtn");
  busy(btn, true, "创建中…");
  post("/api/register", {username:u, password:p, password2:p2}, function(r){
    if(!r.ok){ busy(btn, false); toast(r.msg, false); return; }
    post("/api/login", {username:u, password:p}, function(r2){
      busy(btn, false);
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

function openModal(which){
  $("overlay").hidden = false;
  $("modalPw").hidden = which !== "pw";
  $("modalDel").hidden = which !== "del";
  var first = which === "pw" ? $("pwOld") : $("delPass");
  setTimeout(function(){ first.focus(); }, 40);
}

function closeModal(){
  $("overlay").hidden = true;
  $("modalPw").hidden = true;
  $("modalDel").hidden = true;
}

function doChangePass(){
  var btn = $("pwBtn");
  busy(btn, true, "保存中…");
  post("/api/change_password", {
    token: TOKEN,
    old_password: $("pwOld").value,
    password: $("pwNew").value,
    password2: $("pwNew2").value
  }, function(r){
    busy(btn, false);
    toast(r.msg, r.ok);
    if(r.ok){
      $("pwOld").value = ""; $("pwNew").value = ""; $("pwNew2").value = "";
      closeModal();
    }
  });
}

function doDelete(){
  var btn = $("delBtn");
  busy(btn, true, "删除中…");
  post("/api/delete_account", {token:TOKEN, password:$("delPass").value}, function(r){
    busy(btn, false);
    toast(r.msg, r.ok);
    if(r.ok){
      TOKEN = "";
      localStorage.removeItem("oo_token");
      closeModal();
      refresh();
    }
  });
}

function showAuth(){
  $("authView").hidden = false;
  $("acctView").hidden = true;
}

function showAcct(a){
  $("authView").hidden = true;
  $("acctView").hidden = false;
  var name = a.username || "";
  $("acctName").textContent = name;
  $("avatar").textContent = name.charAt(0) || "?";
  $("iAccount").textContent = a.account_id;
  $("iUid").textContent = a.uid;
  $("iCreated").textContent = a.created_text;
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

document.addEventListener("keydown", function(e){
  if(e.key === "Escape") closeModal();
});

get("/api/server/info", function(r){
  if(r && r.data && r.data.server_name){ $("srvName").textContent = r.data.server_name; }
});

refresh();
"""


def _page(body):
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<title>OpenOrange 账号中心</title><style>' + CSS + "</style></head><body>"
        '<div class="shell">' + BRAND + "<main class=\"content\">" + body + "</main></div>"
        + OVERLAY
        + '<div id="toast"></div>'
        "<script>" + SCRIPT + "</script></body></html>"
    )


def render_index():
    return _page(BODY)




# ---------------------------------------------------------------- 管理控制台
ADMIN_CSS = r"""
:root{
  --bg:#08090c; --text:#f4f5f7; --muted:#8b909c;
  --line:rgba(255,255,255,.08);
  --accent:#ff9f43; --accent2:#ff6a00;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
html,body{height:100%}
body{
  margin:0;background:var(--bg);color:var(--text);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;
}
body::before{
  content:"";position:fixed;inset:0;pointer-events:none;
  background:radial-gradient(900px 600px at 82% -12%,rgba(255,106,0,.08),transparent 62%);
}
.shell{position:relative;display:flex;flex-direction:column;height:100vh}
.top{
  display:flex;align-items:center;justify-content:space-between;
  height:58px;padding:0 20px;border-bottom:1px solid var(--line);flex:none;
}
.brand{display:flex;align-items:center;gap:11px;font-size:15px;font-weight:600}
.mark{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;
      background:linear-gradient(135deg,var(--accent),var(--accent2));
      color:#1a0f05;font-size:11px;font-weight:800;letter-spacing:.5px}
.top-right{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--muted)}
.top-right b{color:var(--text)}
.badge{padding:3px 9px;border-radius:20px;font-size:11.5px;border:1px solid var(--line)}
.badge.gm{color:#ffd08a;border-color:rgba(255,159,67,.4);background:rgba(255,159,67,.1)}
.badge.admin{color:#8ce0a8;border-color:rgba(70,196,122,.4);background:rgba(70,196,122,.1)}
.badge.player{color:var(--muted)}
a.ghost{
  padding:6px 13px;border-radius:9px;border:1px solid var(--line);
  background:rgba(255,255,255,.04);color:#c9ced8;text-decoration:none;font-size:13px;
}
a.ghost:hover{background:rgba(255,255,255,.08);color:var(--text)}
.body{flex:1;display:flex;min-height:0}
.console{flex:1;display:flex;flex-direction:column;min-width:0}
.out{
  flex:1;overflow-y:auto;padding:16px 20px 8px;
  font-family:Consolas,ui-monospace,"Courier New",monospace;
  font-size:13.5px;line-height:1.75;
}
.block{margin-bottom:14px}
.cmdline{color:var(--accent);white-space:pre-wrap;word-break:break-all}
.cmdline .ts{color:#5f6672;margin-right:8px}
.res{color:#d3d8e0;white-space:pre-wrap;word-break:break-all;margin-top:2px}
.res.err{color:#ff8f8f}
.res .empty{color:#5f6672}
.input-row{
  display:flex;align-items:center;gap:10px;padding:14px 20px 18px;flex:none;
  border-top:1px solid var(--line);background:rgba(255,255,255,.015);
}
.prompt{color:var(--accent);font-family:Consolas,monospace;font-size:15px;font-weight:700}
#cmd{
  flex:1;padding:12px 14px;border-radius:11px;border:1px solid rgba(255,255,255,.1);
  background:rgba(255,255,255,.035);color:var(--text);
  font-family:Consolas,ui-monospace,monospace;font-size:14px;
  transition:border-color .18s,box-shadow .18s;
}
#cmd:focus{outline:none;border-color:rgba(255,159,67,.6);
           box-shadow:0 0 0 4px rgba(255,106,0,.12)}
#cmd::placeholder{color:#4d535e}
#send{
  padding:0 20px;height:44px;border:0;border-radius:11px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:#1a0f05;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer;
  transition:transform .16s,opacity .16s;
}
#send:hover{transform:translateY(-1px)}
#send[disabled]{opacity:.5;cursor:default;transform:none}
.quick{
  width:230px;flex:none;border-left:1px solid var(--line);
  overflow-y:auto;padding:14px 12px;background:rgba(255,255,255,.012);
}
.quick h4{margin:0 0 8px;font-size:11.5px;font-weight:600;color:#6c7280;
          letter-spacing:.6px;text-transform:uppercase}
.qbtn{
  display:block;width:100%;text-align:left;margin-bottom:6px;padding:8px 10px;
  border-radius:9px;border:1px solid var(--line);background:rgba(255,255,255,.03);
  color:#c2c8d2;font-family:Consolas,monospace;font-size:12.5px;cursor:pointer;
  transition:.15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.qbtn:hover{background:rgba(255,255,255,.07);color:var(--text);border-color:rgba(255,159,67,.35)}
.qbtn small{display:block;color:#6c7280;font-family:inherit;font-size:11px;margin-top:2px}
.denied{
  flex:1;display:grid;place-items:center;text-align:center;padding:40px;
}
.denied h2{margin:0 0 10px;font-size:20px}
.denied p{margin:0;color:var(--muted);font-size:13.5px;line-height:1.8}
@media (max-width:820px){ .quick{display:none} }
"""

ADMIN_BODY = r"""
<div class="shell">
  <div class="top">
    <div class="brand"><span class="mark">OO</span><span>管理控制台</span></div>
    <div class="top-right">
      <span>已登录 <b id="who">-</b></span>
      <span class="badge player" id="perm">-</span>
      <a class="ghost" href="/">返回门户</a>
    </div>
  </div>

  <div class="body" id="body">
    <div class="console">
      <div class="out" id="out"></div>
      <div class="input-row">
        <span class="prompt">&gt;</span>
        <input id="cmd" autocomplete="off" spellcheck="false" placeholder="输入 /help 开始">
        <button id="send" onclick="submitCmd()">执行</button>
      </div>
    </div>
    <aside class="quick" id="quick"></aside>
  </div>

  <div class="denied" id="denied" hidden>
    <div>
      <h2>没有访问权限</h2>
      <p>这个页面需要 GM 及以上权限。<br>请让管理员执行 <code>/account 你的账号 perm 1</code>。</p>
    </div>
  </div>
</div>
"""

ADMIN_SCRIPT = r"""
var TOKEN = localStorage.getItem("oo_token") || "";
var PERM = 0;
var HIST = [];
var HIDX = -1;
var COMMANDS = window.__OO_COMMANDS__ || [];

function $(id){ return document.getElementById(id); }

function esc(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function nowTs(){
  var d = new Date();
  function z(n){ return n < 10 ? "0" + n : "" + n; }
  return z(d.getHours()) + ":" + z(d.getMinutes()) + ":" + z(d.getSeconds());
}

function append(cmdText, lines, ok){
  var box = document.createElement("div");
  box.className = "block";
  var head = document.createElement("div");
  head.className = "cmdline";
  head.innerHTML = '<span class="ts">[' + nowTs() + ']</span>' + esc(cmdText);
  box.appendChild(head);
  var body = document.createElement("div");
  body.className = "res" + (ok ? "" : " err");
  if (lines && lines.length){
    body.textContent = lines.join("\n");
  } else {
    body.innerHTML = '<span class="empty">(无输出)</span>';
  }
  box.appendChild(body);
  var out = $("out");
  out.appendChild(box);
  out.scrollTop = out.scrollHeight;
}

function post(path, data, cb){
  var x = new XMLHttpRequest();
  x.open("POST", path, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var r = null;
    try { r = JSON.parse(x.responseText); } catch(e){ r = null; }
    cb(r || {ok:false, msg:"服务器返回异常"});
  };
  x.send(JSON.stringify(data || {}));
}

function submitCmd(){
  var el = $("cmd");
  var text = el.value.trim();
  if(!text) return;
  el.value = "";
  HIST.push(text);
  if(HIST.length > 200) HIST.shift();
  HIDX = -1;

  append(text, ["…"], true);
  var box = $("out").lastChild;
  post("/api/admin/command", {token:TOKEN, text:text}, function(r){
    box.parentNode.removeChild(box);
    if(r && r.data && r.data.clear){
      $("out").innerHTML = "";
      return;
    }
    append(text, r.lines || [r.msg || "无输出"], r.ok);
  });
}

function renderQuick(){
  var box = $("quick");
  var html = "<h4>快捷命令</h4>";
  var presets = ["/help", "/who", "/server", "/search 长命锁", "/account @"];
  for(var i=0;i<presets.length;i++){
    html += '<button class="qbtn" onclick="fill(this)">' + esc(presets[i]) + "</button>";
  }
  html += "<h4 style='margin-top:16px'>全部命令</h4>";
  for(var j=0;j<COMMANDS.length;j++){
    var c = COMMANDS[j];
    var tip = c.implemented ? "" : "<small>未实现</small>";
    html += '<button class="qbtn" onclick="fill(this)" data-cmd="/' + esc(c.name) + '">/' + esc(c.name) + tip + "</button>";
  }
  box.innerHTML = html;
}

function fill(btn){
  var el = $("cmd");
  el.value = btn.getAttribute("data-cmd") || btn.textContent;
  el.focus();
}

function complete(){
  var el = $("cmd");
  var v = el.value;
  if(v.charAt(0) !== "/") return;
  var word = v.slice(1).split(/\s+/)[0];
  if(!word) return;
  var hits = [];
  for(var i=0;i<COMMANDS.length;i++){
    if(COMMANDS[i].name.indexOf(word) === 0) hits.push(COMMANDS[i].name);
  }
  if(hits.length === 1){
    el.value = "/" + hits[0] + " ";
  } else if(hits.length > 1){
    append("/" + word, ["候选：" + hits.join("  /")], true);
  }
}

document.addEventListener("keydown", function(e){
  var el = $("cmd");
  if(e.target !== el) return;
  if(e.key === "Enter"){ submitCmd(); return; }
  if(e.key === "Tab"){ e.preventDefault(); complete(); return; }
  if(e.key === "ArrowUp"){
    e.preventDefault();
    if(!HIST.length) return;
    if(HIDX < 0) HIDX = HIST.length - 1; else if(HIDX > 0) HIDX--;
    el.value = HIST[HIDX] || "";
    return;
  }
  if(e.key === "ArrowDown"){
    e.preventDefault();
    if(HIDX < 0) return;
    HIDX++;
    if(HIDX >= HIST.length){ HIDX = -1; el.value = ""; return; }
    el.value = HIST[HIDX] || "";
  }
});

function boot(){
  if(!TOKEN){
    $("denied").hidden = false;
    $("body").hidden = true;
    return;
  }
  post("/api/me", {token:TOKEN}, function(r){
    if(!r || !r.ok){
      $("denied").hidden = false;
      $("body").hidden = true;
      return;
    }
    var a = r.account || {};
    PERM = a.perm_level || 0;
    if(PERM < 1){
      $("denied").hidden = false;
      $("body").hidden = true;
      return;
    }
    $("who").textContent = a.username || "";
    var badge = $("perm");
    badge.className = "badge " + (PERM >= 2 ? "admin" : "gm");
    badge.textContent = PERM >= 2 ? "管理员" : "GM";
    renderQuick();
    append("/help", [], true);
    post("/api/admin/command", {token:TOKEN, text:"/help"}, function(r2){
      $("out").innerHTML = "";
      append("/help", r2.lines || [], r2.ok);
    });
    $("cmd").focus();
  });
}

boot();
"""


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
        '<title>归橙 · 管理控制台</title><style>' + ADMIN_CSS + "</style></head><body>"
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
            print("[web] register %s username=%s -> %s" % (msg, username, ok), flush=True)
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
            print("[web] login %s username=%s -> %s" % (msg, username, ok), flush=True)
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
            perm = db.get_perm_level(username)
            ctx = cmds.Context(username, perm, source="chat", server_name=_server_name())
            text = str(req.get("text") or "")
            res = cmds.execute(text, ctx)
            print("[chatcmd] %s(perm=%d) > %s -> %s" % (username, perm, text, res.ok), flush=True)
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
            print("[admin] %s(perm=%d) > %s -> %s" % (acc["username"], perm, text, res.ok), flush=True)
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
                print("[launcher] login BLOCKED username=%s" % username, flush=True)
                self._json({"code": 0, "msg": "失败次数过多，请稍后再试", "data": {}})
                return

            # 只校验密码，不建网页会话 —— 启动器不需要会话，
            # 建了反而会把网页端正在登录的那一端挤掉。
            ok, msg, acc = models.verify_credentials(username, password)
            if not ok:
                _note_login_fail(username)
                print("[launcher] login REJECTED username=%s: %s" % (username, msg), flush=True)
                self._json({"code": 0, "msg": msg, "data": {}})
                return

            ret, err = models.login(username)
            if err:
                _note_login_fail(username)
                print("[launcher] login FAILED username=%s: %s" % (username, err), flush=True)
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
            print("[web] change_password username=%s -> %s" % (acc["username"], ok), flush=True)
            self._json({"ok": ok, "msg": msg})
            return

        if path == "/api/delete_account":
            acc = models.whoami(req.get("token") or "")
            if not acc:
                self._json({"ok": False, "msg": "未登录"}, 401)
                return
            ok, msg = models.remove_account(acc["username"], req.get("password"))
            print("[web] delete_account username=%s -> %s" % (acc["username"], ok), flush=True)
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
