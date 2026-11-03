/* 管理控制台前端逻辑（P0-E3 从 web/app.py 抽出） */
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

/* 登录态判定 —— 页面共用唯一入口。
   /api/me 只挂在 do_POST 路由里（GET 会 404），返回 {ok, account:{username, perm_level}}。
   以前每个页面各写一份，mail.js 就因为用 GET + 把 account 当字符串而误判"没有权限"。 */
function checkLogin(onOk, onDenied){
  if(!TOKEN){ onDenied(); return; }
  post("/api/me", { token: TOKEN }, function(r){
    if(!r || !r.ok){ onDenied(); return; }
    var a = r.account || {};
    PERM = a.perm_level || 0;
    if(PERM < 1){ onDenied(); return; }
    onOk(a, PERM);
  });
}
window.OO_CHECK_LOGIN = checkLogin;

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

/* ★ 只在控制台页面自动启动。admin.js 现在也被邮件页内联（为了共用 checkLogin/post），
   邮件页没有 #cmd，直接跑 boot() 会在 renderQuick 里对 null 设 innerHTML 而中断。 */
if($("cmd")) boot();
