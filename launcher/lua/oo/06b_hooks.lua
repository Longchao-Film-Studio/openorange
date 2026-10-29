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

    -- ★ 2026-10-24 重做（回滚 5916c65 后的正确方案）：hosts 黑洞官方 serverlist 后，
    --   updateServerInfo → QueryServerList → QueryUrl 连拒超时（转圈几秒）→ onQueryServerList(非0)
    --   → Toast 178 且不显示服务器名。
    --   替换 updateServerInfo：不联网，直接灌列表 + 走正常成功路径（onQueryServerList(0)），
    --   转圈事件立即由 EndWeakNetOnFree 结束，服务器名立即可见。
    --   本块整体在 loginpanel 结构已验证的 pc.selector 模式内；全程 pcall，不裸 error。
    if type(methods.updateServerInfo) == 'function' then
      methods.updateServerInfo = function(self2)
        pcall(function()
          install_server_list()
          self2:onQueryServerList(0)
        end)
      end
      LOG('lua: loginpanel.updateServerInfo 已替换（本地列表直显，不联网）')
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
    -- ★ P0-账号绑定（2026-10-23）：给 NEW_CONNECTION（reqType=0）补账号。
    --
    --   背景：客户端原生 GateClient:OnSessionKeyOk() 建第一条连接时把 auth 写死
    --   成 ""（core/client/gateclient.lua 的 NEW_CONNECTION 分支），服务端只能靠
    --   「ms query 时序 + 30 秒窗口队列」猜是哪个账号，两个客户端同时登录会串号。
    --
    --   这里在 connectServer 出口把 auth 换成 Mailbox{1:"account_<account_id>"}
    --   （服务端 gate.py 的 uid_from_auth 能剥前缀还原），让服务端直接认号。
    --
    --   ★ 为什么不触发「repeat attr」：这里不改 m.connectServer（那会走 classType
    --     的 __newindex 校验，游戏报 class ... has repeat attr），而是拿到
    --     底层 vtbl 表（classType.__objMetatable.__index）直接 rawset。
    --     loginpanel 包装 onRefreshServerList / setServerText 用的就是同一个套路。
    local vtbl = nil
    pcall(function()
      local mt = rawget(m, '__objMetatable')
      if type(mt) == 'table' then vtbl = mt.__index end
    end)
    if type(vtbl) == 'table' and type(vtbl.connectServer) == 'function' then
      local orig_connectServer = vtbl.connectServer
      vtbl.connectServer = function(self, reqType, auth, entMbBin, version)
        if reqType == 0 then
          -- ★ 只改第一条连接；bindAccount(4)/bindAvatar(3) 的 auth 原样保留
          --   （那条链路客户端本来就回传正确的 mailbox）。
          local acct = pc.account_id
          if type(acct) == 'string' and acct ~= '' then
            -- Mailbox{1:"account_<account_id>"} 的 protobuf 字节
            local name = 'account_' .. acct
            auth = string.char(0x0A, #name) .. name
            LOG('authbind: reqType=0 auth -> account_' .. acct)
          end
        end
        return orig_connectServer(self, reqType, auth, entMbBin, version)
      end
      LOG('lua: GateClientRpcHandler.connectServer 已包裹（reqType=0 带账号）')
    else
      LOG('lua: GateClientRpcHandler vtbl 不可用，跳过 auth 注入（回退猜队列）')
    end
    LOG('lua: GateClientRpcHandler 已加载')

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
  -- (9) 推送订阅者修正：addPushEndpoint 的目标从「组件」换成 ClientAvatar
  --
  -- 背景（M6 推送）：客户端 bind 成功后调
  --   serviceMgr:addPushEndpoint(self.msCallerId, self.pushHandler or self)
  -- 其中 self 是 ClientAvatarMsCommon 组件；而 MailService_newMailNotice 是
  -- 经 class.AddComponent 混入到 **ClientAvatar 类** 上的（core/framework/class.lua:491-531），
  -- 组件实例上没有 -> MsServiceManager.onPush 里 target[method] == nil -> 消息被丢弃，
  -- 日志只会出现 "onPush to <uid> for method MailService_newMailNotice not defined"。
  --
  -- ★ 为什么不直接 setPushHandler：那要在 bindCb 之前设置，而 bindCb 由组件自己调；
  --   这里改注册目标更稳，也不依赖调用顺序。
  -- ★ 为什么 rawset：class.Class 的 __newindex 对已存在成员会
  --   error("class ... has repeat attr ...")，这正是本文件 (5)(8) 记过的坑。
  elseif key == 'core.microservice.msservicemanager' and not pc.pushfix then
    pc.pushfix = true
    local orig_add = rawget(m, 'addPushEndpoint')
    if type(orig_add) == 'function' then
      rawset(m, 'addPushEndpoint', function(self, id, entity)
        local ok, GD = pcall(original_require, 'Core.Client.GlobalData')
        local av = nil
        if ok and type(GD) == 'table' then av = GD.Avatar end
        if av ~= nil and entity ~= av then
          LOG('pushfix: 订阅者 ' .. tostring(id) .. ' -> ClientAvatar')
          entity = av
        end
        return orig_add(self, id, entity)
      end)
      LOG('lua: addPushEndpoint 已挂钩（推送订阅者 -> ClientAvatar）')
    else
      LOG('lua: addPushEndpoint 不存在，跳过推送修正')
    end
  end

  return m
end

-- ------------------------------------------------------------
-- 暴露给原生的入口
-- ------------------------------------------------------------
OO_Internal_Ready   = true
OO_Internal_Require = require
