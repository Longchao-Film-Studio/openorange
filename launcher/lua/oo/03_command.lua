-- oo/03_command.lua —— 游戏内指令通道（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- 玩家在聊天框输入 / 开头的消息时不发聊天服务，改走本机后端的
-- /api/chat/command。认证用启动器签发的 ticket（客户端手里已经有）。
-- 结果用 Toast 显示 —— 第二参传 0 时第三参就是自由文本。

-- ------------------------------------------------------------
-- 游戏内指令通道
-- ------------------------------------------------------------
-- 玩家在聊天框输入以 / 开头的消息时，不发聊天服务，改走本机后端的
-- /api/chat/command。认证用启动器签发的 ticket（客户端手里已经有）。
-- 结果用 Toast 显示 —— 第二参传 0 时第三参就是自由文本。

local function oo_toast(text)
  if type(text) ~= 'string' or text == '' then return end
  pcall(function()
    ui.postEvent(EUIEventDefine.EReceiveTips, UIGlobalConst.EUITips.Toast, 0, text)
  end)
end

local function oo_json_escape(s)
  s = tostring(s or '')
  s = s:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
  return s
end

local function oo_json_get_str(text, key)
  local pat = '"' .. key .. '":"((\\.|[^"\\])*)"'
  local v = text:match(pat)
  if not v then return nil end
  v = v:gsub('\\"', '"'):gsub('\\\\', '\\')
  v = v:gsub('\\n', '\n'):gsub('\\r', '\r'):gsub('\\t', '\t')
  return v
end

-- 极简 HTTP POST（LuaSocket 是客户端自带的，Prism 调试通道也用它）
local function oo_http_post(url, body)
  local ok, socket = pcall(original_require, 'socket')
  if not ok or type(socket) ~= 'table' then
    return nil, 'socket 不可用'
  end
  local host, port, path = url:match('^http://([^:/]+):(%d+)(/.*)$')
  if not host then
    return nil, 'web_base 格式不对: ' .. tostring(url)
  end
  local tcp = socket.tcp()
  tcp:settimeout(2)
  local c, err = tcp:connect(host, tonumber(port))
  if not c then
    pcall(function() tcp:close() end)
    return nil, 'connect 失败: ' .. tostring(err)
  end
  local req = 'POST ' .. path .. ' HTTP/1.1\r\n'
    .. 'Host: ' .. host .. ':' .. port .. '\r\n'
    .. 'Content-Type: application/json\r\n'
    .. 'Content-Length: ' .. tostring(#body) .. '\r\n'
    .. 'Connection: close\r\n\r\n' .. body
  local sok, serr = tcp:send(req)
  if not sok then
    pcall(function() tcp:close() end)
    return nil, 'send 失败: ' .. tostring(serr)
  end
  local chunks = {}
  while true do
    local s, e, partial = tcp:receive(4096)
    local data = s or partial
    if data and #data > 0 then chunks[#chunks + 1] = data end
    if e then break end
  end
  pcall(function() tcp:close() end)
  local resp = table.concat(chunks)
  local sep = resp:find('\r\n\r\n', 1, true)
  if not sep then return nil, '响应格式不对' end
  return resp:sub(sep + 4)
end

local function oo_command_dispatch(msg)
  if not pc.ticket or not pc.web_base then
    oo_toast('✗ 指令通道未就绪')
    return
  end
  local body = '{"ticket":"' .. oo_json_escape(pc.ticket)
    .. '","text":"' .. oo_json_escape(msg) .. '"}'
  local resp, herr = oo_http_post(pc.web_base .. '/api/chat/command', body)
  if not resp then
    oo_toast('✗ ' .. tostring(herr))
    return
  end
  local chat = oo_json_get_str(resp, 'chat')
  LOG('cmd: ' .. tostring(msg) .. ' -> ' .. tostring(chat))
  oo_toast(chat or '✗ 无返回')
end

local function deliver(raw)
  if type(raw) ~= 'string' or raw == '' then return end

  local ok, result = pcall(function()
    return original_require('Modules.SDK.FPXJSON').decode(raw)
  end)
  if not ok or type(result) ~= 'table' then
    LOG('deliver: decode failed: ' .. tostring(result))
    return
  end

  -- 与官服 fpxsdkreceiver.lua 第 17 行同样的判据
  if result.code ~= 1 then
    LOG('deliver: server returned code=' .. tostring(result.code)
      .. ' msg=' .. tostring(result.msg))
    return
  end

  -- 记下票据与后端地址：游戏内指令通道要用它们回连 /api/chat/command
  if type(result.data) == 'table' then
    pc.ticket = result.data.ticket
    pc.web_base = result.data.web_base
    -- ★ P0-账号绑定：connectServer 的 NEW_CONNECTION 要拿 account_id 当 auth
    --   （06b_hooks 的 authbind 用它拼 Mailbox{1:"account_<account_id>"}）。
    pc.account_id = result.data.account_id
    LOG('account: command channel ready, web_base=' .. tostring(pc.web_base))
  end

  -- 交给游戏自己的账号处理：fpxaccount.lua M.onSdkLogin(bInitGame, result)
  -- 与官方 modules/sdk/fpxsdkreceiver.lua:16 一致，传 pc.initialized。
  --
  -- ★ EnterGame() 不是「进入游戏世界」，而是 main.lua:74 的
  --   「启动 patch 阶段 -> GameInit -> GameStart」，走完才到 LoginStage 的登录面板，
  --   那里才有「开始游戏」按钮。所以它必须**永远**调用，
  --   否则客户端根本到不了那个按钮（会卡在启动画面然后超时退出）。
  --   --auto 控制的是「要不要替玩家点那个按钮」，见 require 里 loginpanel 的钩子。
  local _auto = rawget(_G, 'OO_AUTO') == true
  LOG('deliver: auto=' .. tostring(_auto))
  local ok2, err = pcall(function()
    FPXAccount.onSdkLogin(pc.initialized, result)
  end)
  if not ok2 then
    LOG('deliver: onSdkLogin failed: ' .. tostring(err))
    -- ★ 手动模式：此刻 ui 全局还没建好（实测 FPXAccount.lua:57 报 ui 为 nil），
    --   所以不能立刻发 ESdkLogin。改成：先把账号信息交给 FPXAccount（不触发事件），
    --   这里不再做「等 ui 就绪后补发事件」的尝试：实测补发不会让登录面板出现按钮，
    --   客户端会停在启动画面。正确做法是让 EnterGame() 正常跑完（见下）。
    return
  end

  LOG('account: sdk login delivered account_id='
    .. tostring(result.data and result.data.account_id))

  -- 与官服 fpxsdkreceiver.lua:17-20 完全一致
  if not pc.initialized and not pc.entered then
    pc.entered = true
    EnterGame()
    LOG('account: EnterGame invoked')
  end
end
