-- oo/06_hooks.lua —— require 接管与入口（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- 回调包装、phonestcore 的 gate RPC 注册，以及最关键的 require 接管：
-- 在游戏加载每个模块时插钩子（SDK 适配、登录面板、聊天指令通道……）。
-- 文件末尾暴露 OO_Internal_Ready / OO_Internal_Require 给原生 DLL。

local function install_cb_wrappers()
  for _, nm in ipairs(CB_NAMES) do
    local mark = '__oo_cbwrapped_' .. nm
    local f = rawget(_G, nm)
    if type(f) == 'function' and not rawget(_G, mark) then
      rawset(_G, mark, true)
      rawset(_G, nm, function(...)
        -- CallbackManagerHandler 每秒被调一次，借它当心跳做增量注册
        if nm == 'CallbackManagerHandler' then
          pcall(register_entity_names)
          pcall(bump_lua_log_level)
          pcall(enable_table_dump)
          -- 等界面起来几帧再反射 C# 类型，太早 CS 可能还没初始化完
          pc.frames = (pc.frames or 0) + 1
          if pc.frames == 90 then pcall(probe_cs_api) end
          if pc.frames == 60 then pcall(probe_phonestcore_keys) end
          if (pc.frames % 30) == 0 then pcall(probe_street_actor) end
          if (pc.frames % 30) == 15 then pcall(probe_p1_accept) end
          -- 端到端 RPC 探针默认关（会连发 20 条，刷日志）；需要时改成 pcall(probe_p1_setrpc)
          if OO_PROBE_RPC then pcall(probe_p1_setrpc) end
          if (pc.frames % 30) == 25 then pcall(probe_encode_response) end
          if (pc.frames % 30) == 10 then pcall(probe_userdata) end
          -- 写操作探针默认关（会改数据）；需要时打开
          if OO_PROBE_WRITE then
            if (pc.frames % 30) == 5 then pcall(probe_useitem) end
            if (pc.frames % 30) == 8 then pcall(probe_upgrade) end
          end
          if (pc.frames % 30) == 12 then pcall(probe_sysunlock) end
          if (pc.frames % 30) == 18 then pcall(probe_charbag) end
          if (pc.frames % 30) == 24 then pcall(probe_story) end
          -- 街区切换探针默认关（会主动发 RPC 干扰正常流程）；排障时置 true
          if OO_PROBE_STREET then
            if (pc.frames % 30) == 27 then pcall(probe_street_switch) end
          end
          if OO_PROBE_STORY_ADV then
            if (pc.frames % 30) == 28 then pcall(probe_story_advance) end
          end
          if OO_PROBE_STORY_NPC then
            if (pc.frames % 30) == 29 then pcall(probe_story_npc) end
          end
          if OO_PROBE_STARTGAME then
            if (pc.frames % 30) == 5 then pcall(probe_startgame) end
          end
          if OO_PROBE_ENTERHUB then
            if (pc.frames % 30) == 6 then pcall(probe_enter_hub) end
          end
          if OO_PROBE_HUBSTEPS then
            if (pc.frames % 30) == 7 then pcall(probe_hub_steps) end
          end
          if OO_PROBE_LOADSTATE then
            if (pc.frames % 30) == 8 then pcall(probe_loading_state) end
          end
          if OO_PROBE_LOADSTEP then
            if (pc.frames % 30) == 9 then pcall(probe_loadstep) end
          end
          if OO_PROBE_PRELOAD then
            if (pc.frames % 30) == 11 then pcall(install_preload_hook) end
          end
          if OO_PANEL_TIMEOUT then
            if (pc.frames % 30) == 12 then pcall(install_panel_timeout) end
          end
          if OO_PROBE_NEXTLEVEL then
            if (pc.frames % 30) == 20 then pcall(probe_next_level) end
          end
          -- 地图探针默认关（会自动弹地图，干扰操作）；排障时置 true
          if OO_PROBE_MAP then
            if (pc.frames % 30) == 20 then pcall(probe_openmap) end
          end
          pcall(probe_sig_watch)
        end
        local n = select('#', ...)
        local parts = {}
        for i = 1, math.min(n, 5) do
          parts[#parts + 1] = tostring(select(i, ...))
        end
        LOG('cb: ' .. nm .. '(' .. table.concat(parts, ', ') .. ')')
        return f(...)
      end)
      LOG('lua: 全局回调已包装 ' .. nm)
    end
  end
end

-- ------------------------------------------------------------
-- 注册 gate / ms 的 RPC 名字到原生索引表
-- ------------------------------------------------------------
-- 背景：原生层用 phonestcore.RpcIndex 做「索引 -> 名字」的反查，
-- 日志里能看到大量
--     [rpc index 2119322371 not found]   ← connectResponse
--     [rpc index 1378977164 not found]   ← sendRequestEncryptToken
--     [rpc index 889905872 not found]    ← bindAccount
-- 说明这张表是空的。全库只有 Core.Common.EntityFactory 会调
-- phonestcore.registerRpc（那是给实体 RPC 用的），gate/ms 的 RPC 名字没人注册。
--
-- registerRpc(name) 会返回该名字的 32 位索引，顺带把名字登记进表里。
-- 这里在 phonestcore 首次被 require 时补注册一遍。
local GATE_RPC_NAMES = {
  'connectServer', 'sendConnectServer', 'connectResponse', 'confirmEncryptKeyAck',
  'requestEncryptToken', 'sendRequestEncryptToken', 'responseEncryptToken',
  'createChannelEntity', 'entityMessage', 'entityMessageResponse', 'channelMessage',
  'frameMessage', 'gateMessage', 'gateFrameMessage',
  'bindAccount', 'bindAvatar', 'bindSoul', 'unbindSoul', 'disconnectClient',
  'recvHeartbeat', 'sendHeartbeat', 'setHeartbeatCb',
  'propertySync2Client', 'propertyStrategySync2Client',
  'onSyncProperty', 'onStrategySyncProperty',
  'sendMicroRequest', 'onMicroResponse', 'onMicroPush', 'onConfirmEncryptKeyAck',
  'onProtoExceedLimit', 'onSessionDisconnected',
}

-- ------------------------------------------------------------
-- 实体 RPC 应答名（_SC）补注册（历史遗留，保留但已不是关键路径）
-- ------------------------------------------------------------
-- 依据 core/common/entityfactory.lua:97-122：
--   EntityFactory._parseMethod 只遍历 config 里 5 个 accessor 段
--   （ClientOnlyMsg / ServerOnlyMsg / ChannelMsg / EngineMsg / GmMethod），
--   对每个名字调 phonestcore.registerRpc。所有 _SC 应答方法都不在这 5 段里，
--   所以原生 RpcIndex 查不到它们的名字，实机日志：
--     [PHONESTCORE_HOOK] [rpc index 405867711 not found]
--       [RpcIndex.cpp:31(phonestcore::RpcIndex::getRpcName)]
--
-- ★ 但补注册**并不够**：把应答塞进 entityMessage 时，名字能查到之后客户端又报
--     [RpcCall] Entity(avatar_2000) CallRpc Error.
--       Function(RpcGetSunActLevel_SC) Is Not Rpc.
--   因为 entityMessage 一律走 CallRpc 查 EntityFactory.rpc_methods，而 _SC
--   从来没被注册成 rpc_method。
--   真正的应答通道是独立的 entityMessageResponse（帧类型 3，字段 1=rid、
--   字段 2=msgpack[retStatus, response]），见
--   core/client/gateclientrpchandler.lua:128-150 —— 那条路不需要名字。
--   这里保留注册只是让日志干净些，别再指望它解决派发问题。
local SERVER_RPC_SC_NAMES = {
  'RpcGetSunActLevel_SC',
  'RpcGetMatchDuration_SC',
}

-- ------------------------------------------------------------
-- 注册实体类名到原生索引表
-- ------------------------------------------------------------
-- createChannelEntity 的 ChannelEntityInfo.type 是 uint32，但 Lua 侧
-- GateClientRpcHandler:createChannelEntity(entityType, ...) 把它直接传给
-- EntityFactory.createEntity(name, entityId) —— 也就是**类名字符串**。
--
-- 实测（tools/phonest_probe.py）：
--   genRpcIndex("ClientAvatar") = 1292148699
--   registerRpc("ClientAvatar") 之后 getRpcName(1292148699) -> "ClientAvatar"
--
-- 所以：实体类型号 = genRpcIndex(类名)，原生层靠 RpcIndex 表反查回类名。
-- 不注册类名，原生层就还原不出名字，createChannelEntity 必然失败。
local function register_gate_rpcs(ph)
  if pc.rpcreg or type(ph) ~= 'table' then return end
  pc.rpcreg = true

  -- ★ 收集客户端注册的**全部** RPC 名。
  --
  --   phonestcore 内部维护「名字 <-> 32 位索引」双向表：
  --     registerRpc(name)   客户端自己往里注册
  --     getRpcName(index)   反查（实测 ClientAvatar -> 1292148699 反查=ClientAvatar）
  --   所以 hook 住 registerRpc，就能把客户端认识的名字全收下来 ——
  --   这是运行时的权威注册表，比扫源码调用点可靠（源码扫描漏了 _SC 回调名）。
  --
  --   日志前缀 rpcname| ，跑完去重即可。
  if type(ph.registerRpc) == 'function' and not pc.rpcname_hooked then
    pc.rpcname_hooked = true
    local orig_reg = ph.registerRpc
    ph.registerRpc = function(name, ...)
      local r = orig_reg(name, ...)
      if type(name) == 'string' then
        LOG('rpcname| ' .. name)
      end
      return r
    end
    LOG('lua: phonestcore.registerRpc 已包裹（收集全部 RPC 名）')
  end

  local ok_n, fail_n = 0, 0
  local all = {}
  for _, nm in ipairs(GATE_RPC_NAMES) do all[#all + 1] = nm end
  for _, nm in ipairs(SERVER_RPC_SC_NAMES) do all[#all + 1] = nm end
  for _, nm in ipairs(all) do
    local ok, idx = pcall(ph.registerRpc, nm)
    if ok then
      ok_n = ok_n + 1
      if nm == 'connectResponse' or nm == 'sendRequestEncryptToken'
         or nm == 'bindAccount' or nm == 'confirmEncryptKeyAck' then
        LOG('rpcreg: ' .. nm .. ' -> ' .. tostring(idx))
      end
    else
      fail_n = fail_n + 1
    end
  end
  LOG('rpcreg: 注册完成 ok=' .. ok_n .. ' fail=' .. fail_n)
end

-- ------------------------------------------------------------
-- 接管 require
-- ------------------------------------------------------------
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
