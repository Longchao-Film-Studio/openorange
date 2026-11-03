/* 发送邮件页面前端（2026-11-03） */
var TOKEN = localStorage.getItem("oo_token") || "";
var TEMPLATES = window.__OO_MAIL_TEMPLATES__ || [];
var ITEMS = window.__OO_ITEMS__ || [];
var HISTORY = [];
var CUR_TPL = 0;

function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

var toastTimer = null;
function toast(msg, isErr){
  var el = $("toast");
  el.textContent = msg;
  el.className = "toast on" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function(){ el.className = "toast"; }, 2600);
}
function setStatus(msg, kind){
  var el = $("status");
  el.textContent = msg || "";
  el.className = "status " + (kind || "");
}

function renderTplList(){
  var kw = ($("tplSearch").value || "").trim().toLowerCase();
  var box = $("tplList");
  var html = '<div class="tpl' + (CUR_TPL === 0 ? " on" : "") + '" data-id="0">' +
             '<span class="id">—</span><span class="tt">纯自定义（不用模板）</span>' +
             '<span class="it">无头像</span></div>';
  var shown = 0;
  for(var i=0;i<TEMPLATES.length;i++){
    var t = TEMPLATES[i];
    var hay = (t.id + " " + (t.title||"")).toLowerCase();
    if(kw && hay.indexOf(kw) < 0) continue;
    shown++;
    var n = 0; for(var k in (t.items||{})) n++;
    html += '<div class="tpl' + (CUR_TPL === t.id ? " on" : "") + '" data-id="' + t.id + '">' +
            '<span class="id">' + t.id + '</span>' +
            '<span class="tt">' + esc(t.title || "(无标题)") + '</span>' +
            '<span class="it">' + (n ? n + " 项附件" : "无附件") + '</span></div>';
  }
  if(!shown && kw) html += '<div class="empty">没有匹配的模板</div>';
  box.innerHTML = html;
  var nodes = box.querySelectorAll(".tpl");
  for(var j=0;j<nodes.length;j++){
    nodes[j].onclick = function(){ pickTemplate(parseInt(this.getAttribute("data-id"),10) || 0); };
  }
  var cur = tplById(CUR_TPL);
  $("tplHint").textContent = CUR_TPL
    ? ("已套用 #" + CUR_TPL + " " + (cur ? cur.title : ""))
    : "点一下套用；不选 = 纯自定义";
}

function tplById(id){
  for(var i=0;i<TEMPLATES.length;i++){ if(TEMPLATES[i].id === id) return TEMPLATES[i]; }
  return null;
}

function toggleTpl(){
  var box = $("tplList");
  var folded = box.className.indexOf("fold") >= 0;
  box.className = folded ? "tpl-list" : "tpl-list fold";
  $("tplFold").textContent = folded ? "收起" : "展开";
}

function pickTemplate(id){
  CUR_TPL = id;
  renderTplList();
  if(id){
    var t = tplById(id);
    if(t){
      $("title").value = t.title || "";
      $("sender").value = t.sender || "";
      $("content").value = t.content || "";
      setItems(t.items || {});
    }
  }
  refreshPreview();
}

function addItem(iid, num){
  var box = $("items");
  var row = document.createElement("div");
  row.className = "item-row";
  row.innerHTML =
    '<div class="picker"><input type="text" class="iname" autocomplete="off" ' +
      'placeholder="道具名或 ID" value="' + esc(iid ? itemLabel(iid) : "") + '" data-id="' + (iid||"") + '">' +
      '<div class="picker-list"></div></div>' +
    '<input type="text" class="inum" placeholder="数量" value="' + esc(num || "") + '">' +
    '<button class="del" title="删除">×</button>';
  box.appendChild(row);
  row.querySelector(".del").onclick = function(){ row.remove(); refreshPreview(); };
  var inp = row.querySelector(".iname");
  bindPicker(inp, row.querySelector(".picker-list"));
  inp.addEventListener("input", function(){ inp.setAttribute("data-id",""); refreshPreview(); });
  row.querySelector(".inum").addEventListener("input", refreshPreview);
  if(!iid) inp.focus();
  refreshPreview();
}

function itemLabel(id){
  var idn = parseInt(id, 10);
  for(var i=0;i<ITEMS.length;i++){ if(ITEMS[i][0] === idn) return ITEMS[i][1] + " (" + idn + ")"; }
  return String(id);
}

function bindPicker(inp, list){
  var cur = -1;
  function close(){ list.className = "picker-list"; cur = -1; }
  function show(){
    var kw = inp.value.trim().toLowerCase();
    if(!kw){ close(); return; }
    var hits = [];
    for(var i=0;i<ITEMS.length && hits.length<40;i++){
      var it = ITEMS[i];
      if(String(it[0]).indexOf(kw) === 0 || it[1].toLowerCase().indexOf(kw) >= 0){ hits.push(it); }
    }
    if(!hits.length){ close(); return; }
    var html = "";
    for(var j=0;j<hits.length;j++){
      html += '<div data-id="' + hits[j][0] + '" data-name="' + esc(hits[j][1]) + '">' +
              esc(hits[j][1]) + '<em>' + hits[j][0] + '</em></div>';
    }
    list.innerHTML = html;
    list.className = "picker-list on";
    cur = -1;
    var nodes = list.querySelectorAll("div");
    for(var k=0;k<nodes.length;k++){
      nodes[k].onmousedown = function(ev){
        ev.preventDefault();
        inp.value = this.getAttribute("data-name") + " (" + this.getAttribute("data-id") + ")";
        inp.setAttribute("data-id", this.getAttribute("data-id"));
        close(); refreshPreview();
      };
    }
  }
  inp.addEventListener("input", show);
  inp.addEventListener("focus", show);
  inp.addEventListener("blur", function(){ setTimeout(close, 120); });
}

function setItems(obj){
  $("items").innerHTML = "";
  var any = false;
  for(var k in obj){ if(obj.hasOwnProperty(k)){ addItem(k, obj[k]); any = true; } }
  if(!any) refreshPreview();
}

function readItems(){
  var rows = $("items").querySelectorAll(".item-row");
  var out = [];
  for(var i=0;i<rows.length;i++){
    var inp = rows[i].querySelector(".iname");
    var num = (rows[i].querySelector(".inum").value || "").trim();
    var raw = inp.getAttribute("data-id") || "";
    var id = raw ? parseInt(raw, 10) : parseInt(inp.value,10);
    if(!id && !num) continue;
    if(!/^\d+$/.test(String(id)) || !/^\d+$/.test(num)) return null;
    out.push(id + ":" + num);
  }
  return out.join(",");
}

function refreshPreview(){
  var t = tplById(CUR_TPL);
  var title = $("title").value.trim() || (t ? t.title : "");
  var sender = $("sender").value.trim() || (t ? t.sender : "");
  var body = $("content").value || (t ? t.content : "");
  $("pvTitle").textContent = title || "（标题）";
  $("pvSender").textContent = sender || "（发件人）";
  $("pvBody").textContent = body || "（正文）";
  $("lenTip").textContent = ($("content").value || "").length + " 字";
  var raw = readItems();
  var items = null;
  if(raw === null) items = null;
  else if(raw === "") items = (t && t.items) ? t.items : {};
  else { items = {}; var ps = raw.split(",");
         for(var i=0;i<ps.length;i++){ var kv = ps[i].split(":"); items[kv[0]] = kv[1]; } }
  var aw = $("pvAward"), box = $("pvItems");
  var keys = items ? Object.keys(items) : [];
  if(!keys.length){ aw.style.display = "none"; }
  else {
    var html = "";
    for(var j=0;j<keys.length;j++){
      html += '<span class="it">' + esc(itemLabel(keys[j]).replace(/\s*\(\d+\)$/, "")) +
              '<b>×' + esc(items[keys[j]]) + '</b></span>';
    }
    box.innerHTML = html; aw.style.display = "";
  }
  $("pvNoIcon").style.display = (CUR_TPL === 0) ? "" : "none";
}

function sendMail(){
  var uid = ($("uid").value || "").trim();
  if(!uid){ toast("先填目标账号", true); $("uid").focus(); return; }
  var items = readItems();
  if(items === null){ toast("附件填错了：道具和数量都要是数字", true); return; }
  var body = { token: TOKEN, uid: uid, template: CUR_TPL,
               count: parseInt($("count").value, 10) || 1 };
  var t = $("title").value.trim();   if(t) body.title = t;
  var s = $("sender").value.trim();  if(s) body.sender = s;
  var c = $("content").value;        if(c) body.content = c;
  if(items !== "") body.items = items;
  var btn = $("sendBtn");
  btn.disabled = true;
  setStatus("发送中…", "busy");
  post("/api/admin/mail/send", body, function(r){
    btn.disabled = false;
    if(!r.ok){
      var msg = r.msg || r.error || "发送失败";
      setStatus("✗ " + msg, "err"); toast(msg, true);
      logHistory(false, uid, body, msg);
      return;
    }
    var d = r.data || {};
    var pushed = !!d.pushed;
    setStatus((pushed ? "✓ 已发送并推送" : "✓ 已发送（客户端不在线）") +
              " · MailId " + (d.mail_ids || []).join(","), "ok");
    toast("已发送给 " + uid + (pushed ? "，客户端已收到推送" : ""), false);
    logHistory(true, uid, body, null, d);
    if($("paneBox").style.display !== "none") openBox();
  });
}

function resetForm(){
  $("title").value = ""; $("sender").value = ""; $("content").value = "";
  $("count").value = "1"; CUR_TPL = 0;
  $("items").innerHTML = "";
  renderTplList(); refreshPreview(); setStatus("", "");
}

function logHistory(ok, uid, body, err, data){
  var d = new Date();
  HISTORY.unshift({
    ok: ok, uid: uid, title: body.title || ("模板 " + body.template),
    tpl: body.template, err: err, pushed: !!(data && data.pushed),
    hh: ("0"+d.getHours()).slice(-2) + ":" + ("0"+d.getMinutes()).slice(-2)
  });
  if(HISTORY.length > 30) HISTORY.pop();
  renderHistory();
}

function renderHistory(){
  var box = $("paneHist");
  if(!HISTORY.length){ box.innerHTML = '<div class="empty">还没有发送记录</div>'; return; }
  var html = "";
  for(var i=0;i<HISTORY.length;i++){
    var h = HISTORY[i];
    html += '<div class="mail"><div class="l1"><span class="tt">' + esc(h.title) +
      '</span><span class="mid">' + h.hh + '</span></div>' +
      '<div class="l2"><span>' + esc(h.uid) + '</span>' +
      '<span>' + (h.tpl ? "模板 " + h.tpl : "纯自定义") + '</span>' +
      (h.ok ? '<span class="tag done">' + (h.pushed ? "已推送" : "已发") + '</span>'
            : '<span class="tag un">失败</span>') + '</div>' +
      (h.ok ? "" : '<div class="l2" style="color:#e2726b">' + esc(h.err) + '</div>') +
      '</div>';
  }
  box.innerHTML = html;
}

function switchTab(which){
  var isHist = which === "hist";
  $("tabHist").className = isHist ? "on" : "";
  $("tabBox").className  = isHist ? "" : "on";
  $("paneHist").style.display = isHist ? "" : "none";
  $("paneBox").style.display  = isHist ? "none" : "";
  if(!isHist) openBox();
}

function openBox(){
  var uid = ($("uid").value || "").trim();
  if(!uid){ $("paneBox").innerHTML = '<div class="empty">先在上面填目标账号</div>'; return; }
  $("paneBox").innerHTML = '<div class="empty">读取中…</div>';
  post("/api/admin/mail/list", { token: TOKEN, uid: uid }, function(r){
    var box = $("paneBox");
    if(!r.ok){ box.innerHTML = '<div class="empty" style="color:#e2726b">' + esc(r.msg || "查询失败") + '</div>'; return; }
    var d = r.data || {};
    if(d.exists === false){ box.innerHTML = '<div class="empty">这个账号还没有存档</div>'; return; }
    var mails = d.mails || [];
    if(!mails.length){ box.innerHTML = '<div class="empty">邮箱是空的</div>'; return; }
    var html = "";
    for(var i=0;i<mails.length;i++){
      var m = mails[i];
      html += '<div class="mail"><div class="l1"><span class="tt">' + esc(m.title) +
        '</span><span class="mid">#' + m.mail_id + '</span></div>' +
        '<div class="l2"><span>' + esc(m.src || "") + '</span>' +
        (m.gift_received ? '<span class="tag done">已领</span>' : '<span class="tag un">未领</span>') +
        (m.have_read ? '<span class="tag read">已读</span>' : '') +
        (m.has_gift ? '<span class="tag read">带附件</span>' : '') + '</div></div>';
    }
    box.innerHTML = html;
  });
}

function boot(){
  if(!TOKEN){ $("denied").hidden = false; $("body").style.display = "none"; return; }
  if(typeof window.OO_CHECK_LOGIN !== "function"){
    $("deniedMsg").innerHTML = "页面脚本加载不完整（缺少 admin.js）。<br>请强制刷新 Ctrl+F5。";
    $("denied").hidden = false; $("body").style.display = "none";
    return;
  }
  var dl = $("uidList"), uh = "", uids = window.__OO_UIDS__ || [];
  for(var i=0;i<uids.length;i++){ uh += '<option value="' + esc(uids[i]) + '">'; }
  dl.innerHTML = uh;
  renderTplList();
  $("tplSearch").addEventListener("input", renderTplList);
  ["title","sender"].forEach(function(id){ $(id).addEventListener("input", refreshPreview); });
  $("content").addEventListener("input", refreshPreview);
  $("uid").addEventListener("change", function(){
    if($("paneBox").style.display !== "none") openBox();
  });
  renderHistory();
  refreshPreview();
  document.addEventListener("keydown", function(ev){
    if((ev.ctrlKey || ev.metaKey) && ev.key === "Enter"){ ev.preventDefault(); sendMail(); }
  });
  window.OO_CHECK_LOGIN(function(a, perm){
    $("who").textContent = a.username || "-";
    $("perm").textContent = (perm >= 2 ? "管理员" : "GM");
    $("uid").focus();
  }, function(){
    $("denied").hidden = false;
    $("body").style.display = "none";
  });
}

boot();