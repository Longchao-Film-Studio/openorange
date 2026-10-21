-- oo/06a_hooks.lua + 06b_hooks.lua —— require 接管与入口（P0-E3 从 oo_bootstrap.lua 拆出）
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
