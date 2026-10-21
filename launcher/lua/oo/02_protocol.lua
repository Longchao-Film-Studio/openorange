-- oo/02_protocol.lua —— 协议层（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- 网络加密开关、实例方法追踪、自动重试、phonestcore / ms 编码探测、
-- ServerList 注入。这些改的是「怎么和服务器说话」。

-- ============================================================
-- 服务器列表注入
-- ------------------------------------------------------------
-- 实测：SERVER_AREA=official, IsNativePkg=false
--   => ServerListManager:onDone 里的 mergeLocal() 分支不会执行
--   => 官方服务器列表地址（Switch.server_list_host 里那两个）又不可达
--   => G.servermgr 始终为空 => loginProd() 里 GetOneServer() 返回 nil
--      => 点"开始游戏"只会弹 Toast 179，永远连不到任何服务器
--
-- 所以必须由我们直接把列表灌进去。走 ParseFromLoadService 是为了
-- 复用游戏自己的 parseContent 解析（字段校验、Server/Region 对象构造、
-- ms_gate 三级回退、rid→region 关联，全部由官方代码完成）。
-- ============================================================
local SERVER_LIST_JSON = '{"clientGate":"127.0.0.1:33100",'
  .. '"regions":[{"rid":10,"name":"OpenOrange","clientGate":"127.0.0.1:33100"}],'
  .. '"servers":[{"hostnum":2000,"name":"OpenOrange","status":1,"openTime":0,"rid":10,'
  .. '"clientGate":"127.0.0.1:33100",'
  .. '"clusters":[{"clusterId":20000,"status":1,"addrs":["127.0.0.1:33101"]}]}]}'

-- ------------------------------------------------------------
-- 关闭网络层加密（开发期）
-- ------------------------------------------------------------
-- 依据 gateclient.lua L29-30 / L77-81：
--   self.enable_encrypt = self.config.enableEncrypt
--   if self.enable_encrypt then handshake(pubKey) else OnSessionKeyOk() end
-- config 是 NetHandler.config 这个普通 Lua 表（NetHandler:ctor 里 self.config = config or {}），
-- 因此直接改字段即可让客户端跳过 RSA 握手，直接发明文 msgpack 的 sendConnectServer。
-- 这样我们不必实现 RSA 交换就能先看清上层 RPC 结构。
-- 注意：只在本地开发服使用；对官方网关无效（对端仍会走加密协议）。
local function disable_net_encryption()
  if pc.noenc then return end
  pc.noenc = true

  pcall(function()
    local CR = original_require('Core.Client.ClientRepo')
    local nh = CR and CR.netHandler
    if nh and type(nh.config) == 'table' then
      nh.config.enableEncrypt = false
      nh.config.enableMsEncrypt = false
      LOG('noenc: netHandler.config.enableEncrypt=false')
    else
      LOG('noenc: netHandler.config 不可用')
    end
  end)

  for _, mod in ipairs({ 'Net.NetConf', 'Net.net_conf', 'Core.Net.NetConf' }) do
    local ok, c = pcall(original_require, mod)
    if ok and type(c) == 'table' then
      c.enableEncrypt = false
      c.enableMsEncrypt = false
      LOG('noenc: ' .. mod .. '.enableEncrypt=false')
    end
  end
end

-- ------------------------------------------------------------
-- 实例级追踪
-- ------------------------------------------------------------
-- 为什么必须在实例上包：
--   LoginManager 通过 class.AddComponents(LoginManager, index) 把
--   ServerListComp 的方法**拷贝**进了自己的类，所以对模块表
--   (Manager.Login.ServerListComp) 上的函数做 wrapper 对实例无效 ——
--   实测 "slc: LoginGameServer" 从未打印就是证据。
--   直接给实例赋同名函数可以覆盖元表查找，才拦得住。
-- ------------------------------------------------------------
local function wrap_method(obj, name, tag)
  local f = obj[name]
  if type(f) ~= 'function' then
    LOG('trace: ' .. tag .. '.' .. name .. ' 不存在')
    return
  end
  obj[name] = function(self2, ...)
    LOG('trace: ' .. tag .. '.' .. name .. '(' .. argstr(...) .. ')')
    local r = f(self2, ...)
    local extra = ''
    if name == 'GetServer' and type(r) == 'table' then
      extra = ' [name=' .. tostring(r.name)
        .. ' ms_gate=' .. tostring(r.ms_gate)
        .. ' rid=' .. tostring(r.rid) .. ']'
    end
    LOG('trace: ' .. tag .. '.' .. name .. ' -> ' .. tostring(r) .. extra)
    return r
  end
end

local function install_instance_tracers(G)
  if pc.insthook then return end
  pc.insthook = true
  if type(G) ~= 'table' then return end

  if type(G.loginmgr) == 'table' then
    wrap_method(G.loginmgr, 'GetChooseServer', 'loginmgr')
    wrap_method(G.loginmgr, 'SetChooseServer', 'loginmgr')
    wrap_method(G.loginmgr, 'GetDefaultServer', 'loginmgr')

    -- ★ 关键拦截点：LoginGameServer 是"开始游戏"真正发起连接的地方。
    --
    -- 为什么必须在这里强制重灌：
    --   官方服务器列表地址仍然存活，
    --   QueryServerList 会把我们的列表整个清掉（QueryUrl 里 self.servers = {}），
    --   然后 GetDefaultServer() 推荐官方服「九龙街区」
    --   （网关地址由官方 serverlist 下发），
    --   客户端于是拿着我们伪造的 ticket 去连官方网关，必然被拒，
    --   服务端回 OnTicketFailed → 弹「登录认证失败, 请重新登录。」
    --   实测证据：GetServer(2000) -> nil / GetServer(10) -> 九龙街区。
    local orig_login_game = G.loginmgr.LoginGameServer
    if type(orig_login_game) == 'function' then
      G.loginmgr.LoginGameServer = function(self2, ...)
        pcall(disable_net_encryption)
        pcall(function() G.servermgr:ParseFromLoadService(SERVER_LIST_JSON) end)
        pcall(function() self2.choose_hostnum = 2000 end)
        pcall(function() G.hostnum = 2000 end)
        LOG('trace: LoginGameServer 强制注入并选中 2000，当前 GetOneServer='
          .. tostring(G.servermgr and G.servermgr:GetOneServer())
          .. ' choose=' .. tostring(self2.choose_hostnum))

        -- 直接走网关连接，跳过 C# 的 QueryLoadService。
        -- 原因：QueryLoadService 是 C# 实现，会先向 ms_gate 发一条微服务查询
        -- （实测 6 字节：02 00 00 00 01 00），我们不实现它就走不下去。
        -- 而下一跳 debugLoginPointedGameServer 是纯 Lua（serverlistcomp.lua L66-76），
        -- 只要求 pointedServer 有 clusterId 和 gates，可以直接构造并调用，
        -- 从而把链路推进到真正的网关 TCP 连接。
        -- 不再绕过 QueryLoadService：让它走官方路径，
        -- 以便抓取明文（已关闭加密）的微服务 callService 请求。
        -- 依据 manager/login/loadcomp.lua：
        --   QueryLoadService → msNetworkEventCallback:start({ms_gate})
        --     → QueueService:Query → msProxy:callService("GameLoadService","query",{Hostnum,Username})
        --     → onGetServerGates → ParseFromLoadService(response.ServerList)
        --     → networkEventCallback:start(cluster_id, cluster.gates, ticket, cb)
        LOG('trace: 调用原始 LoginGameServer（走官方 ms 路径）')
        local okc, errc = pcall(orig_login_game, self2, ...)
        LOG('trace: 原始 LoginGameServer 返回 ok=' .. tostring(okc)
          .. ' err=' .. tostring(errc))
        return true
      end
    end
  end
  if type(G.servermgr) == 'table' then
    wrap_method(G.servermgr, 'GetServer', 'servermgr')
    wrap_method(G.servermgr, 'GetOneServer', 'servermgr')

    -- ★★ 关键修复：在官方列表拉取**结束的那一刻**就把我们的列表盖回去。
    --
    -- 时序问题（loginpanel.lua L257-263 / L267-286）：
    --   updateServerInfo() → QueryServerList → 官方列表成功
    --     → ServerListManager:QueryUrl 里 self.servers = {}  ← 我们的被清空
    --     → onDone() → callback(0) → Panel:onQueryServerList(0)
    --     → setServerText() → GetDefaultServer() → 显示官方服「九龙街区」
    -- 而 LoginGameServer 里的注入发生在"点开始游戏"之后 —— 太晚了，
    -- 登录面板上显示的名字早就被改成官方的了。
    --
    -- onDone 正好在 QueryServerList 的回调链最前端，在这里盖回去，
    -- 后面的 setServerText() 拿到的就是我们的服务器。
    local orig_on_done = G.servermgr.onDone
    if type(orig_on_done) == 'function' then
      G.servermgr.onDone = function(self2, err)
        -- 先让官方流程走完（它会清空 servers/regions）
        local r = orig_on_done(self2, err)
        -- 再无条件盖回我们的列表
        pcall(function()
          self2:ParseFromLoadService(SERVER_LIST_JSON)
          self2.merged_by_oo = true
        end)
        pcall(function() G.loginmgr.choose_hostnum = 2000 end)
        pcall(function() G.hostnum = 2000 end)
        pcall(function() G.loginmgr:SetChooseServer(2000) end)
        LOG('serverlist: onDone 后强制盖回 —— servers 数='
          .. tostring(self2.servers and (function()
               local n = 0; for _ in pairs(self2.servers) do n = n + 1 end; return n
             end)() or '?')
          .. ' GetOneServer=' .. tostring(self2:GetOneServer())
          .. ' 名字=' .. tostring((self2:GetServer(2000) or {}).name))
        return r
      end
      LOG('lua: servermgr.onDone 已包裹（列表保护）')
    else
      LOG('lua: servermgr.onDone 不存在，跳过包裹')
    end
  end
  LOG('lua: 实例级追踪已安装')
end

-- ------------------------------------------------------------
-- 自动点「开始游戏」（仅 --auto 启用）
-- ------------------------------------------------------------
-- 历史：这段原本是早期爆破阶段的「自动重连实验」——服务端会轮换不同的
--   connectResponse 应答变体，客户端每 8 秒自动重连一次，一次运行就能把
--   候选类型号试完，不用人工反复点击。
-- 现在：它实际上就是「自动点开始游戏」——每 4 秒调一次
--   G.loginmgr:LoginGameServer()（等价于登录面板的 _btn_login），
--   最多 6 次。
-- ★ 必须只在 --auto 时启动，否则不加 --auto 也会自动进游戏。
-- 用游戏自带的 TimerManager（loginpanel.lua L425 同款），
-- 不去劫持 GameTick，避免影响主循环。
local function start_auto_retry(G)
  if pc.autoretry then return end
  pc.autoretry = true
  pc.auto_tries = 0

  local ok = pcall(function()
    TimerManager.addRepeatTimer(4, function()
      if pc.auto_tries >= 6 then return end   -- 只试 6 次，避免触发 C# 解析器崩溃
      pc.auto_tries = pc.auto_tries + 1
      LOG('auto: 第 ' .. pc.auto_tries .. ' 次重连尝试')
      pcall(function() G.loginmgr:LoginGameServer() end)
    end)
  end)
  LOG('lua: 自动重连实验启动 ok=' .. tostring(ok))
end

-- ------------------------------------------------------------
-- 结果投递：把服务端返回的 JSON 喂给游戏自身的账号层
-- ------------------------------------------------------------
-- ------------------------------------------------------------
-- 枚举 phonestcore 原生模块的全部成员
-- ------------------------------------------------------------
-- 目的：phonestcore 是编译进 xlua.dll 的 phonest RPC 框架（客户端+服务端两套 API）。
-- 我们需要知道：
--   1) 有没有 HandlerType* 系列常量（决定能否在进程内起服务端）
--   2) 有没有消息类型常量（可能直接给出 connectResponse 的类型号）
--   3) 有哪些 ConType* / 配置项
-- 纯读取，不创建任何对象，风险极低。
local function dump_phonestcore()
  if pc.phdumped then return end
  pc.phdumped = true

  local ok, ph = pcall(original_require, 'phonestcore')
  if not ok then
    LOG('phonestcore: require 抛错: ' .. tostring(ph))
    return
  end
  LOG('phonestcore: type=' .. type(ph))
  if type(ph) ~= 'table' then return end

  local items = {}
  local okIter, errIter = pcall(function()
    for k, v in pairs(ph) do
      local tv = type(v)
      local s = tostring(k) .. ' :: ' .. tv
      if tv == 'number' then
        s = s .. ' = ' .. tostring(v)
      elseif tv == 'string' then
        s = s .. ' = "' .. v .. '"'
      elseif tv == 'boolean' then
        s = s .. ' = ' .. tostring(v)
      end
      items[#items + 1] = s
    end
  end)
  if not okIter then
    LOG('phonestcore: pairs() 失败: ' .. tostring(errIter))
  end

  table.sort(items)
  LOG('===== phonestcore 成员 ' .. #items .. ' 个 =====')
  for _, s in ipairs(items) do
    LOG('  ph| ' .. s)
  end
  LOG('===== phonestcore END =====')

  -- 元表（可能藏着 __index 指向的常量表）
  local okMt, mt = pcall(function() return getmetatable(ph) end)
  if okMt and type(mt) == 'table' then
    LOG('phonestcore: 有元表')
    pcall(function()
      for k, v in pairs(mt) do
        LOG('  phmt| ' .. tostring(k) .. ' :: ' .. type(v))
      end
    end)
  end
end

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

