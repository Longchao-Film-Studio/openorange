/* 账号中心前端逻辑（P0-E3 从 web/app.py 抽出） */
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
