-- oo/02b_protocol.lua —— 02a 的续（同一段协议层代码，按文件名顺序拼接）
-- ------------------------------------------------------------
-- 探测 phonestcore 的 RPC 索引 API
-- ------------------------------------------------------------
-- 成员表里有 genRpcIndex / getRpcName / getSendRpcName / registerRpc，
-- 如果 genRpcIndex("connectResponse") 能返回那个 u16 消息类型号，
-- 我们就能直接构造正确的应答包，不必盲扫（盲扫已导致游戏崩溃）。
-- 每个调用都单独 pcall，任何一项失败不影响其它项。
local function probe_rpc_api()
  if pc.rpcdumped then return end
  pc.rpcdumped = true

  local ok, ph = pcall(original_require, 'phonestcore')
  if not ok or type(ph) ~= 'table' then
    LOG('rpcapi: phonestcore 不可用')
    return
  end
  pcall(install_regrpc_wrapper, ph, 'probe_rpc_api')

  local names = {
    'connectResponse', 'sendConnectServer', 'sendRequestEncryptToken',
    'confirmEncryptKeyAck', 'sendHeartbeat', 'recvHeartbeat', 'heartbeat',
    'bindAccount', 'bindAvatar', 'bindSoul', 'unbindSoul',
    'disconnectClient', 'entityMessage', 'entityMessageResponse',
    'frameMessage', 'channelMessage', 'gateMessage', 'gateFrameMessage',
    'onSyncProperty', 'onStrategySyncProperty', 'OnTicketFailed',
    'OnTicketExpired', 'sendConnect', 'connect', 'login',
  }

  LOG('===== genRpcIndex 探测 =====')
  for _, n in ipairs(names) do
    local ok2, v = pcall(ph.genRpcIndex, n)
    LOG('  rpcIdx| ' .. n .. ' -> ok=' .. tostring(ok2) .. ' val=' .. tostring(v))
  end

  -- getRpcName 传小整数返回空串；改试传"哈希值"，
  -- 因为 genRpcIndex 返回的就是 32 位哈希，getRpcName 很可能是它的反函数。
  LOG('===== getRpcName 反查（传哈希值）=====')
  local hashes = {
    { 'connectResponse', 2119322371 },
    { 'sendConnectServer', 2087431183 },
    { 'sendRequestEncryptToken', 1378977164 },
    { 'confirmEncryptKeyAck', 543775382 },
    { 'bindAccount', 889905872 },
  }
  for _, h in ipairs(hashes) do
    local ok3, v = pcall(ph.getRpcName, h[2])
    LOG('  rpcNameByHash| ' .. h[1] .. '(' .. h[2] .. ') -> ok=' .. tostring(ok3)
      .. ' val=' .. tostring(v))
  end

  -- 也试试十六进制字符串形式
  for _, h in ipairs(hashes) do
    local hexs = string.format('%08X', h[2])
    local ok4, v = pcall(ph.getRpcName, hexs)
    LOG('  rpcNameByHex| ' .. hexs .. ' -> ok=' .. tostring(ok4) .. ' val=' .. tostring(v))
  end

  -- 试无参调用
  local ok5, v5 = pcall(ph.getRpcName)
  LOG('  rpcNameNoArg| ok=' .. tostring(ok5) .. ' val=' .. tostring(v5))

  -- ★ 2026-10-20 移除：原来这里 for i = 0, 2000 扫 getRpcName，
  --   每个未注册的索引都会让原生层打一条
  --     [PHONESTCORE_HOOK] [rpc index N not found]
  --   一次启动就产生 2001 条 CrashSight 异常日志，把真正的错误埋掉了。
  --   这个探测当时是为了搞清「注册序号 vs 哈希」，结论早就有了（是 32 位哈希），
  --   所以整段删掉。

  -- 试 registerRpc 的签名（只观察报错信息，不实际改变行为）
  local okR, vR = pcall(ph.registerRpc)
  LOG('  reg| registerRpc() -> ok=' .. tostring(okR) .. ' val=' .. tostring(vR))
  local okR2, vR2 = pcall(ph.registerRpc, 'OoProbeRpc')
  LOG('  reg| registerRpc("OoProbeRpc") -> ok=' .. tostring(okR2) .. ' val=' .. tostring(vR2))

  -- 注册后立刻反查：如果 getRpcName(哈希) 能返回名字，
  -- 说明哈希就是标识，只是之前没注册过。
  if okR2 and type(vR2) == 'number' then
    local okN, vN = pcall(ph.getRpcName, vR2)
    LOG('  reg| getRpcName(' .. vR2 .. ') = ' .. tostring(vN))
    local okS, vS = pcall(ph.getSendRpcName, vR2)
    LOG('  reg| getSendRpcName(' .. vR2 .. ') = ' .. tostring(vS))
  end

  -- 用哈希去查 getSendRpcName（之前只试过整数序号和字符串）
  LOG('===== getSendRpcName 传哈希 =====')
  for _, h in ipairs(hashes) do
    local okS2, vS2 = pcall(ph.getSendRpcName, h[2])
    LOG('  sendNameByHash| ' .. h[1] .. ' -> ok=' .. tostring(okS2) .. ' val=' .. tostring(vS2))
  end

  LOG('===== getSendRpcName 探测 =====')
  for _, n in ipairs({ 'connectResponse', 'sendConnectServer' }) do
    local ok4, v = pcall(ph.getSendRpcName, n)
    LOG('  sendRpcName| ' .. n .. ' -> ok=' .. tostring(ok4) .. ' val=' .. tostring(v))
  end

  local okv, ver = pcall(ph.version)
  LOG('  version() -> ' .. tostring(ver))

  -- 找 C# 侧的 dispatchRpc 对象。
  -- 思路：RpcHandler:dispatchRpc 的实现是 self.cObj:dispatchRpc(rpcName, ...)，
  -- cObj 由 setCobj() 注入。如果我们能拿到任一 cObj，
  -- 就能让客户端"自己编码并发送"一个 connectResponse 包，
  -- 从而直接看到它的线格式 —— 不必再猜。
  -- 枚举 C# 暴露对象的方法：这是最有可能藏 dispatchRpc 的地方。
  -- 如果能调 networkEventCallback:dispatchRpc("connectResponse", 1)，
  -- 客户端就会自己把这个包编码并发出去，我们抓包即可看到精确线格式。
  LOG('===== 枚举 ClientRepo 各 C# 对象的方法 =====')
  pcall(function()
    local CR = original_require('Core.Client.ClientRepo')
    for _, key in ipairs({ 'networkEventCallback', 'msNetworkEventCallback',
                           'msProxy', 'protoCodec', 'httpdns', 'netHandler' }) do
      local obj = CR[key]
      LOG('  obj| --- ClientRepo.' .. key .. ' (' .. type(obj) .. ') ---')
      if type(obj) == 'table' then
        local names = {}
        pcall(function()
          for k, v in pairs(obj) do
            names[#names + 1] = tostring(k) .. ':' .. type(v)
          end
        end)
        local mt = getmetatable(obj)
        if type(mt) == 'table' and type(mt.__index) == 'table' then
          pcall(function()
            for k, v in pairs(mt.__index) do
              names[#names + 1] = '(idx)' .. tostring(k) .. ':' .. type(v)
            end
          end)
        end
        table.sort(names)
        for i = 1, math.min(#names, 60) do
          LOG('    obj| ' .. names[i])
        end
        LOG('    obj| 共 ' .. #names .. ' 项')
      end
    end
  end)

  LOG('===== 探查 ClientRepo / NetHandler 内部对象 =====')
  pcall(function()
    local CR = original_require('Core.Client.ClientRepo')
    for _, k in ipairs({ 'netHandler', 'networkEventCallback', 'msNetworkEventCallback',
                         'protoCodec', 'msProxy', 'httpdns' }) do
      local v = CR[k]
      LOG('  cr| ClientRepo.' .. k .. ' = ' .. type(v))
    end
    local nh = CR.netHandler
    if type(nh) == 'table' then
      for _, k in ipairs({ 'gateClient', 'gateClientSoul', 'config', 'deviceid',
                           'ip', 'port', 'clusterId', 'connectToken' }) do
        local v = nh[k]
        LOG('  nh| netHandler.' .. k .. ' = ' .. type(v)
          .. (type(v) == 'string' and (' "' .. v:sub(1, 32) .. '"') or '')
          .. (type(v) == 'number' and (' ' .. tostring(v)) or ''))
      end
      if type(nh.gateClient) == 'table' then
        local gc = nh.gateClient
        for _, k in ipairs({ 'handler', 'cObj', 'channel', 'status', 'ip', 'port' }) do
          LOG('  gc| gateClient.' .. k .. ' = ' .. type(gc[k]))
        end
        if type(gc.handler) == 'table' then
          LOG('  gc| handler.cObj = ' .. type(gc.handler.cObj))
        end
      end
    end
  end)
end

-- ------------------------------------------------------------
-- 让客户端自己编码一个 onMicroResponse
-- ------------------------------------------------------------
-- 依据 msgateclientrpchandler.lua：
--   dispatchRpc 是双向的 —— 客户端调 dispatchRpc("sendMicroRequest", ...) 发出请求，
--   服务端则调 dispatchRpc("onMicroResponse", ...) 发回应答。
-- 我们在客户端进程里直接调 handler:dispatchRpc("onMicroResponse", rid, params, svc)，
-- 原生层就会按它自己的规则把这个包编码并发出去 ——
-- 抓包即可得到 onMicroResponse 的**确切类型号与字段布局**，不必再猜。
local function probe_ms_encoding()
  if pc.msprobe then return end
  local ok0, CR = pcall(original_require, 'Core.Client.ClientRepo')
  if not ok0 or type(CR) ~= 'table' then return end
  local proxy = CR.msProxy
  if type(proxy) ~= 'table' then return end
  local client = proxy.msclient
  if type(client) ~= 'table' then return end
  local h = client.handler
  if type(h) ~= 'table' then return end
  if type(h.dispatchRpc) ~= 'function' then
    LOG('msprobe: handler 没有 dispatchRpc')
    pc.msprobe = true
    return
  end

  pc.msprobe = true
  LOG('msprobe: 找到 ms handler，开始试编码 onMicroResponse')

  -- 分别试几种参数形态，观察原生层如何编码
  local trials = {
    { 'onMicroResponse', 12345, '', 'ProbeSvc' },
    { 'onMicroResponse', 12345, 'AB', '' },
    { 'sendMicroRequest', 'ProbeSvc', 777, 'probeMethod', 'xx', '', '' },
  }
  for i, t in ipairs(trials) do
    local ok, err = pcall(function() h:dispatchRpc(t[1], t[2], t[3], t[4]) end)
    LOG('msprobe: 第 ' .. i .. ' 次 dispatchRpc("' .. t[1] .. '") ok='
      .. tostring(ok) .. ' err=' .. tostring(err))
  end
end

local function install_server_list()
  if pc.serverlist_done then return end
  local ok, G = pcall(original_require, 'G')
  if not ok or type(G) ~= 'table' or type(G.servermgr) ~= 'table' then
    LOG('serverlist: G.servermgr 不可用，跳过')
    return
  end

  local ok2, err2 = pcall(function()
    G.servermgr:ParseFromLoadService(SERVER_LIST_JSON)
  end)
  if not ok2 then
    LOG('serverlist: ParseFromLoadService failed: ' .. tostring(err2))
    return
  end
  pc.serverlist_done = true

  local one = G.servermgr:GetOneServer()
  LOG('serverlist: installed, GetOneServer=' .. tostring(one))

  local ok3, err3 = pcall(function() G.loginmgr:SetChooseServer(one) end)
  LOG('serverlist: SetChooseServer ok=' .. tostring(ok3)
    .. ' err=' .. tostring(err3)
    .. ' choose=' .. tostring(G.loginmgr.choose_hostnum))

  local okI, errI = pcall(install_instance_tracers, G)
  if not okI then LOG('instance tracers failed: ' .. tostring(errI)) end
  -- ★ 只有 --auto 才启动「自动点开始游戏」。
--   无 --auto 时客户端会停在登录面板（LoginStage）等你手动点那个按钮。
--   以前这个定时器无条件启动，所以不加 --auto 也会自动进游戏。
if rawget(_G, 'OO_AUTO') == true then
  local okA, errA = pcall(start_auto_retry, G)
  LOG('lua: --auto 自动点开始游戏 已启用 ok=' .. tostring(okA))
else
  LOG('lua: 未加 --auto，不自动点开始游戏（停在登录面板等你点）')
end
  if not okA then LOG('auto retry failed: ' .. tostring(errA)) end

  -- 每 2 秒探一次，等 ms 连接建立后立即做编码探针
  pcall(function()
    TimerManager.addRepeatTimer(2, function()
      pcall(probe_ms_encoding)
    end)
  end)
end
