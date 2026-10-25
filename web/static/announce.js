/* 公告 / 喊话页面前端逻辑（2026-10-26） */
var TOKEN = localStorage.getItem("oo_token") || "";
var KINDS = [];
var LIMITS = { banner: 80, toast: 200, gm: 200 };

function $(id){ return document.getElementById(id); }

function esc(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
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

function setStatus(msg, ok){
  var el = $("status");
  el.textContent = msg || "";
  el.className = "ann-status " + (ok ? "ok" : (msg ? "err" : ""));
}

function curKind(){ return $("kind").value; }

function refreshKind(){
  var k = curKind();
  var limit = LIMITS[k] || 80;
  $("limit").textContent = limit;
  var ta = $("text");
  if(ta.value.length > limit){ ta.value = ta.value.slice(0, limit); }
  $("count").textContent = ta.value.length;
  var hint = { banner: "跑马灯：按字数自动算滚动时长，建议 40 字内",
               toast: "Toast：显示在屏幕下方，停留时间由客户端决定",
               gm: "GM 提示：官方定时公告通道（AvatarDebugComp，正式包可能未挂载）" };
  $("kindHint").textContent = hint[k] || "";
  renderPreview();
}

function renderPreview(){
  var text = $("text").value.trim() || "（这里显示发送后的效果）";
  var k = curKind();
  var box = $("preview");
  if(k === "banner"){
    box.innerHTML = '<span class="marquee">' + esc(text) + '</span>';
  } else {
    box.innerHTML = '<span class="ann-toast">' + esc(text) + '</span>';
  }
}

function preset(btn){
  $("text").value = btn.getAttribute("data-text") || btn.textContent;
  var k = btn.getAttribute("data-kind");
  if(k) $("kind").value = k;
  refreshKind();
}

function sendAnnounce(){
  var text = $("text").value.trim();
  if(!text){ setStatus("文案不能为空", false); return; }
  var k = curKind();
  var body = { token: TOKEN, kind: k, text: text };
  var uid = $("uid").value.trim();
  if(uid) body.uid = uid;
  $("send").disabled = true;
  setStatus("发送中…", true);
  post("/api/admin/announce", body, function(r){
    $("send").disabled = false;
    if(r && r.ok){
      var where = r.uid ? ("→ " + r.uid) : ("全服 " + r.targets + " 条连接");
      setStatus("✔ 已发送 " + where, true);
      loadHistory();
    } else {
      setStatus("✗ " + ((r && (r.error || r.msg)) || "发送失败"), false);
    }
  });
}

function renderHistory(items){
  var box = $("history");
  if(!items || !items.length){
    box.innerHTML = '<div class="empty">还没有发送过公告</div>';
    return;
  }
  var html = "";
  for(var i=0;i<items.length;i++){
    var h = items[i];
    var d = new Date((h.ts || 0) * 1000);
    function z(n){ return n < 10 ? "0" + n : "" + n; }
    var ts = z(d.getMonth()+1) + "-" + z(d.getDate()) + " " + z(d.getHours()) + ":" + z(d.getMinutes());
    var meta = ts + " · " + esc(h.kind) + " · " + (h.uid ? ("指定 " + esc(h.uid)) : ("全服 " + h.targets + " 条"));
    var line = h.ok ? ("✔ " + h.ok_count + " 送达") : ("✗ " + esc(h.error || "失败"));
    html += '<div class="item' + (h.ok ? "" : " bad") + '">' +
            '<div class="meta">' + meta + ' · ' + line + '</div>' +
            '<div class="txt">' + esc(h.text) + '</div></div>';
  }
  box.innerHTML = html;
}

function loadHistory(){
  post("/api/admin/announce/history", {token:TOKEN}, function(r){
    if(r && r.ok){
      if(r.kinds && r.kinds.length){
        KINDS = r.kinds;
        for(var i=0;i<KINDS.length;i++){ LIMITS[KINDS[i].kind] = KINDS[i].limit; }
        refreshKind();
      }
      renderHistory(r.history || []);
    }
  });
}

function boot(){
  if(!TOKEN){ $("denied").hidden = false; $("body").hidden = true; return; }
  post("/api/me", {token:TOKEN}, function(r){
    if(!r || !r.ok){ $("denied").hidden = false; $("body").hidden = true; return; }
    var a = r.account || {};
    var perm = a.perm_level || 0;
    if(perm < 1){ $("denied").hidden = false; $("body").hidden = true; return; }
    $("who").textContent = a.username || "";
    var badge = $("perm");
    badge.className = "badge " + (perm >= 2 ? "admin" : "gm");
    badge.textContent = perm >= 2 ? "管理员" : "GM";
    $("kind").onchange = refreshKind;
    $("text").oninput = function(){
      var limit = LIMITS[curKind()] || 80;
      if(this.value.length > limit){ this.value = this.value.slice(0, limit); }
      $("count").textContent = this.value.length;
      renderPreview();
    };
    refreshKind();
    loadHistory();
  });
}

boot();
