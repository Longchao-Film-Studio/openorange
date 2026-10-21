-- oo/06b_hooks.lua —— 06a 的续（同一段 require 接管，按文件名顺序拼接）
function require(name)
  local m = original_require(name)
  pcall(install_cb_wrappers)
  pcall(function()
    local k = string.lower(string.gsub(name, '/', '.'))
    if k == 'phonestcore' then register_gate_rpcs(m) end
  end)
  pcall(register_entity_names)
  local key = string.lower(string.gsub(name, '/', '.'))

  -- (1) FPXSDKAPI：拦 platform/login 与 tracking/notice
  if key == 'modules.sdk.fpxsdkapi' and not pc.api then
    pc.api = true
    local a = FPXSDKAPI
    local call, callstring = a.Call, a.CallString

    a.Call = function(module, fn, payload)
      if module == 'platform' and fn == 'login' then
        -- ★ 劫持**永远**生效，不受 --auto 影响。
        --   以前这里判了 OO_AUTO，不加 --auto 就 return call(...) 放行给原生 SDK，
        --   结果客户端拿着原始参数直连原版服务器 —— 这是错的。
        --   --auto 只控制「拿完凭证后要不要自动进入游戏」，见 deliver() 里的 EnterGame。
        return login()
      end
      if module == 'platform' and (fn == 'logout' or fn == 'unbindPlayer') then
        CS.UnityEngine.Application.Quit()
        return
      end
      if module == 'tracking' or module == 'notice' then return end
      return call(module, fn, payload)
    end

    a.CallString = function(module, fn, payload)
      if fn == 'getTrackingInfo' then
        return OO_Internal_TrackingInfo()
      end
      return callstring(module, fn, payload)
    end

    LOG('lua: account API adapter installed')

  -- (2) FPXSDKReceiver：记录 init 状态
  elseif key == 'modules.sdk.fpxsdkreceiver' and not pc.receiver then
    pc.receiver = true
    local receiver = type(m) == 'table' and m or FPXSDKReceiver
    local set = receiver.SetInitGame
    receiver.SetInitGame = function(value)
      pc.initialized = value
      return set(value)
    end
    LOG('lua: receiver hook installed')

  -- (3) 登录面板：把"刷新服务器列表"重定向到内部选服面板
  elseif key == 'ui.panel.loginpanel.loginpanel' and not pc.selector then
    local methods = rawget(m, '__objMetatable').__index
    if type(methods.onRefreshServerList) ~= 'function'
      or type(methods.openServerSelectPanel) ~= 'function' then
      error('OO_Internal server selector version mismatch: '
        .. 'loginpanel.lua is missing onRefreshServerList/openServerSelectPanel')
    end
    local orig_refresh = methods.onRefreshServerList
    methods.onRefreshServerList = function(self)
      pcall(probe_dump, 'at-refresh-serverlist')
      pcall(install_server_list)
      -- ★ 关键：loginpanel.lua:298-305 的 onRefreshServerList 在正式包里是
      --     if GlobalConst.IsReleasePkg() then self:loginProd()   -- 直接进游戏
      --     else self:openServerSelectPanel() end
      --   实测 IsReleasePkg()=true，所以每次刷新服务器列表都会自动 loginProd()
      --   -> LoginGameServer() -> 连网关 -> 自动进游戏。
      --   这就是「不加 --auto 也会自动进游戏」的真正原因。
      --
      --   不加 --auto 时跳过它：面板停在「服务器 + 开始游戏」页，等玩家自己点。
      if rawget(_G, 'OO_AUTO') ~= true then
        LOG('panel: 未加 --auto，跳过 onRefreshServerList 的自动 loginProd（停在登录面板）')
        return
      end
      return orig_refresh(self)
    end

    -- ★ 兜住 setServerText：它是真正把服务器名写到界面上的地方
    --   （loginpanel.lua L276-286：self._txt_server.text = last_server.name）
    if type(methods.setServerText) == 'function' then
      local orig_sst = methods.setServerText
      methods.setServerText = function(self2, ...)
        pcall(function()
          G.servermgr:ParseFromLoadService(SERVER_LIST_JSON)
          G.loginmgr.choose_hostnum = 2000
          G.hostnum = 2000
        end)
        LOG('panel: setServerText 前已强制盖回列表')
        return orig_sst(self2, ...)
      end
      LOG('lua: loginpanel.setServerText 已包裹')
    end


    -- 追踪"开始游戏"按钮的完整路径
    for _, n in ipairs({ 'onLoginServer', 'loginProd', 'loginDev', 'onSelectServer' }) do
      local f = methods[n]
      if type(f) == 'function' then
        methods[n] = function(self2, ...)
          LOG('panel: ' .. n .. ' (' .. argstr(...) .. ')')
          local r = f(self2, ...)
          LOG('panel: ' .. n .. ' -> ' .. tostring(r))
          return r
        end
      else
        LOG('panel: ' .. n .. ' 不存在')
      end
    end

    pc.selector = true
    LOG('lua: internal server selector installed')

  -- (3b) phonestcore：一被 require 就立刻包住 registerRpc
  elseif key == 'phonestcore' then
    pcall(install_regrpc_wrapper, m, 'require 钩子')

  -- (4) G：初始化完成后立刻探测
  elseif key == 'g' and not pc.ghook then
    pc.ghook = true
    if type(m) == 'table' and type(m.init) == 'function' then
      local init = m.init
      m.init = function(self, ...)
        local ok, err = pcall(init, self, ...)
        if not ok then LOG('probe: G.init error: ' .. tostring(err)) end
        pcall(probe_dump, 'after-G.init')
        pcall(dump_phonestcore)
        pcall(probe_rpc_api)
        pcall(install_server_list)
      end
      LOG('lua: G.init hook installed')
    end

  -- (5) GateClientRpcHandler：不再做方法包装
  --
  -- ★ 2026-10-20 移除：这里原来包了 dispatchRpc 和 bindAccount/bindAvatar/
  --   bindSoul/unbindSoul/disconnectClient，实机日志里立刻出现
  --     class GateClientRpcHandler has repeat attr bindAccount
  --     class GateClientRpcHandler has repeat attr bindAvatar
  --     ...（每个被包的方法一条）
  --   这正是项目里记过的「绝不能包装类方法」——游戏有类属性重复校验。
  --   RPC 收发现在看服务端协议日志就够了（gamesrv_runX.log），
  --   客户端这边不需要再包。
  elseif key == 'core.client.gateclientrpchandler' and not pc.rpchook then
    pc.rpchook = true
    LOG('lua: GateClientRpcHandler 已加载（不做方法包装）')

  -- (5b) ServerListComp：同样不再包装
  --
  -- ★ 2026-10-20 移除：原来包了 LoginGameServer / QueryLoadService /
  --   queryServerList / RefreshServerList / QueryServerList，
  --   实机日志出现 component ServerListComp has repeat attr queryServerList。
  elseif key == 'manager.login.serverlistcomp' and not pc.slchook then
    pc.slchook = true
    LOG('lua: ServerListComp 已加载（不做方法包装）')

  -- (5c) LoginManager / GateClient：不再做方法包装
  --
  -- ★ 教训（2026-10-19）：游戏有类属性重复校验。用 m[n] = wrapper 覆盖类方法会触发
  --     "class GateClient has repeat attr connect"
  --     "class LoginManager has repeat attr LoginFailed"
  --   把链路搞坏。要看这些信息请用 LoggerManager 的日志级别，不要覆盖类方法。
  elseif key == 'manager.loginmanager' and not pc.lmhook then
    pc.lmhook = true
    LOG('lua: LoginManager 已加载（不做方法包装）')

  elseif key == 'core.client.gateclient' and not pc.gchook then
    pc.gchook = true
    LOG('lua: GateClient 已加载（不做方法包装）')

  -- (7) 聊天框指令通道：/ 开头的消息不发聊天，改走本机后端
  elseif key == 'entities.avatarcomponent.avatarchatcomp' and not pc.chathook then
    pc.chathook = true
    if type(m) == 'table' and type(m.AvatarChat_GroupChat_CS) == 'function' then
      local orig_group_chat = m.AvatarChat_GroupChat_CS
      m.AvatarChat_GroupChat_CS = function(self, groupId, contentType, msg, responseCb)
        if type(msg) == 'string' and msg:sub(1, 1) == '/' then
          LOG('cmd: intercepted ' .. msg)
          pcall(oo_command_dispatch, msg)
          if type(responseCb) == 'function' then pcall(responseCb) end
          return
        end
        return orig_group_chat(self, groupId, contentType, msg, responseCb)
      end
      LOG('lua: chat command hook installed')
    else
      LOG('lua: chat command hook failed (AvatarChat_GroupChat_CS missing)')
    end

  -- (8) 只提升日志级别，不做方法包装
  --
  -- ★ 教训：曾经在这里包装 MsClient / ChannelClient / HandlerManager 的方法，
  --   结果触发了游戏的类属性重复校验，日志里出现
  --     "class ChannelClient has repeat attr HandleConnectLost"
  --     "class MsClient has repeat attr OnChannelConnect"
  --   反而把链路搞坏了。以后要看这些，改用 LoggerManager 的日志，
  --   不要覆盖类方法。
  elseif key == 'core.microservice.msclient' and not pc.mshook then
    pc.mshook = true
    pcall(function()
      local LM = original_require('Core.Log.LoggerManager')
      local ph = original_require('phonestcore')
      if type(LM) == 'table' and LM.setLevel and type(ph) == 'table' then
        LM.setLevel(ph.SPDLOG_LEVEL_DEBUG)
        LOG('loglevel: 已设为 DEBUG')
      end
    end)
  end

  return m
end

-- ------------------------------------------------------------
-- 暴露给原生的入口
-- ------------------------------------------------------------
OO_Internal_Ready   = true
OO_Internal_Require = require
