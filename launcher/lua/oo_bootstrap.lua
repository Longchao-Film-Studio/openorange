--[[============================================================
  oo_bootstrap.lua  --  OpenOrange 客户端引导脚本
  ------------------------------------------------------------
  本脚本由 OO_Internal.dll 在游戏进程内通过 lua_load 的 trampoline 注入执行。
  作用：把 FPX 官方登录链路整个接管，改为由本机启动器提供身份，
        使玩家在原版登录面板上点击一次即可进入我们自己的服务器。

  设计来源：对某闭源私服 InternalPC.dll 注入脚本的行为等价重实现。
  与其的差异：
    * 服务器地址不硬编码，全部来自启动器读取的 OpenOrange.ini
    * 保留了官服客户端的方法存在性校验，不匹配时明确报错

  ★ 阶段二新增：只读探测（probe）
    目的：一次性把选服/网络层的运行期真相打到日志里，
         避免反复启动游戏。所有探测都用 pcall 包裹，
         任何一项失败都不会影响正常登录流程。
  ============================================================]]

local original_require = require

-- 全局状态：允许被重复注入时复用
local pc = rawget(_G, 'OO_Internal_State') or {
  initialized = false,   -- 由 FPXSDKReceiver.SetInitGame 反馈
  entered     = false,   -- 是否已经触发过 EnterGame
  pending     = false,   -- 是否正在等待异步登录结果
  api         = false,   -- FPXSDKAPI 适配器是否已安装
  receiver    = false,   -- FPXSDKReceiver 钩子是否已安装
  selector    = false,   -- 选服面板重定向是否已安装
  ghook       = false,   -- G.init 钩子
  rpchook     = false,   -- RPC 层钩子
  gchook      = false,   -- GateClient 钩子
  slchook     = false,   -- ServerListComp 钩子
  lmhook      = false,   -- LoginManager 钩子
  phdumped    = false,   -- phonestcore 成员是否已枚举
  rpcdumped   = false,   -- RPC 索引 API 是否已探测
  regwrapped  = false,   -- registerRpc 包装是否已安装
  chathook    = false,   -- 聊天框指令钩子是否已安装
  ticket      = nil,     -- 启动器签发的登录票据（游戏内指令通道的凭证）
  web_base    = nil,     -- 本机后端地址，如 http://127.0.0.1:8399
  probe_tags  = {},      -- 已执行过的探测标签
}
_G.OO_Internal_State = pc

local function LOG(s)
  pcall(OO_Internal_Log, s)
end

-- ============================================================
-- registerRpc 探针（在脚本加载时立刻安装，抢在一切初始化之前）
-- ------------------------------------------------------------
-- 目的：协议 RPC 的类型号是运行时注册时分配的。
-- 如果注册发生在 Lua 侧（EntityFactory:registerRpc 就是走 Lua 调
-- phonestcore.registerRpc），那么包一层就能看到完整注册序列与返回值。
-- 若一条都不打印，说明注册在 C# 里，这条路也封。
-- ============================================================
-- 实测：脚本加载时 phonestcore 还不可用（module not found），
-- 因此改为在 require 钩子里、以及 probe_rpc_api 里安装包装。
local function install_regrpc_wrapper(ph, where)
  if pc.regwrapped then return end
  if type(ph) ~= 'table' then return end
  if type(ph.registerRpc) ~= 'function' then
    LOG('registerRpc 包装失败(' .. where .. '): registerRpc 不是函数')
    return
  end
  pc.regwrapped = true
  local origReg = ph.registerRpc
  local cnt = 0
  ph.registerRpc = function(name, ...)
    local r = origReg(name, ...)
    cnt = cnt + 1
    if cnt <= 500 then
      LOG('registerRpc[' .. cnt .. ']: ' .. tostring(name) .. ' -> ' .. tostring(r))
    elseif cnt == 501 then
      LOG('registerRpc: 超过 500 条，后续不再打印')
    end
    return r
  end
  LOG('registerRpc wrapper 已安装（' .. where .. '）')
end

-- ============================================================
-- 只读探测
-- ============================================================
local function safe(fn)
  local ok, v = pcall(fn)
  if ok then return v end
  return '<err:' .. tostring(v) .. '>'
end

local function dump_map(t, maxn, fmt)
  if type(t) ~= 'table' then return tostring(t) end
  local out, n = {}, 0
  for k, v in pairs(t) do
    n = n + 1
    if n > maxn then out[#out + 1] = '...'; break end
    local ok, s = pcall(fmt, k, v)
    out[#out + 1] = ok and s or '?'
  end
  if n == 0 then return '(empty)' end
  return table.concat(out, ' | ')
end

local function argstr(...)
  local n = select('#', ...)
  local out = {}
  for i = 1, math.min(n, 5) do
    local v = select(i, ...)
    local t = type(v)
    if t == 'string' then
      out[#out + 1] = 's[' .. #v .. ']:' .. (v:sub(1, 48):gsub('%c', '.'))
    elseif t == 'number' then
      out[#out + 1] = 'n:' .. tostring(v)
    elseif t == 'boolean' then
      out[#out + 1] = 'b:' .. tostring(v)
    else
      out[#out + 1] = t
    end
  end
  return table.concat(out, ', ')
end

local function probe_dump(tag)
  if pc.probe_tags[tag] then return end
  pc.probe_tags[tag] = true

  LOG('===== PROBE [' .. tag .. '] BEGIN =====')

  local GC = rawget(_G, 'GlobalConst')
  if type(GC) == 'table' then
    LOG('probe: SERVER_AREA=' .. tostring(GC.SERVER_AREA))
    LOG('probe: IsNativePkg=' .. tostring(safe(function() return GC.IsNativePkg() end)))
    LOG('probe: IsReleasePkg=' .. tostring(safe(function() return GC.IsReleasePkg() end)))
  else
    LOG('probe: GlobalConst 不存在')
  end

  local Switch = safe(function() return original_require('Switch') end)
  if type(Switch) == 'table' then
    local h = Switch.server_list_host
    if type(h) == 'table' then
      LOG('probe: Switch.server_list_host=' .. table.concat(h, ' , '))
    else
      LOG('probe: Switch.server_list_host=' .. tostring(h))
    end
    LOG('probe: Switch.use_dns=' .. tostring(Switch.use_dns)
      .. ' enable_telnet=' .. tostring(Switch.enable_telnet)
      .. ' ms_key=' .. tostring(Switch.ms_key))
  end

  local ok_ls, ls = pcall(original_require, 'Net.local_server_list')
  LOG('probe: require("Net.local_server_list") ok=' .. tostring(ok_ls)
    .. ' type=' .. type(ls))
  if ok_ls and type(ls) == 'table' then
    LOG('probe: local_server_list servers='
      .. tostring(type(ls.servers) == 'table' and #ls.servers or '?')
      .. ' regions=' .. tostring(type(ls.regions) == 'table' and #ls.regions or '?'))
  end

  local G = safe(function() return original_require('G') end)
  if type(G) == 'table' and type(G.servermgr) == 'table' then
    local sm = G.servermgr
    LOG('probe: servermgr.servers=' .. dump_map(sm.servers, 8, function(k, v)
      return tostring(k) .. '=' .. tostring(v and v.name)
        .. '@' .. tostring(v and v.ms_gate)
        .. '#rid' .. tostring(v and v.rid)
    end))
    LOG('probe: servermgr.regions=' .. dump_map(sm.regions, 8, function(k, v)
      return tostring(k) .. '=' .. tostring(v and v.name)
        .. '@' .. tostring(v and v.ms_gate)
    end))
  else
    LOG('probe: G.servermgr 尚未就绪')
  end

  if type(G) == 'table' and type(G.loginmgr) == 'table' then
    local lm = G.loginmgr
    LOG('probe: loginmgr.choose_hostnum=' .. tostring(lm.choose_hostnum)
      .. ' IsLogin=' .. tostring(safe(function() return lm:IsLogin() end))
      .. ' ticket_len=' .. tostring(lm.ticket and #tostring(lm.ticket) or 0)
      .. ' username=' .. tostring(lm.username))
  end

  local CR = safe(function() return original_require('Core.Client.ClientRepo') end)
  if type(CR) == 'table' then
    LOG('probe: ClientRepo networkEventCallback=' .. type(CR.networkEventCallback)
      .. ' netHandler=' .. type(CR.netHandler)
      .. ' protoCodec=' .. type(CR.protoCodec)
      .. ' msProxy=' .. type(CR.msProxy))
  end

  LOG('===== PROBE [' .. tag .. '] END =====')
end

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
-- 自动重连实验
-- ------------------------------------------------------------
-- 目的：服务端会轮换不同的 connectResponse 应答变体，
-- 客户端每 8 秒自动重连一次，于是一次游戏运行就能把候选类型号试完，
-- 不用人工反复点击。
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
  local okA, errA = pcall(start_auto_retry, G)
  if not okA then LOG('auto retry failed: ' .. tostring(errA)) end

  -- 每 2 秒探一次，等 ms 连接建立后立即做编码探针
  pcall(function()
    TimerManager.addRepeatTimer(2, function()
      pcall(probe_ms_encoding)
    end)
  end)
end

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
    LOG('account: command channel ready, web_base=' .. tostring(pc.web_base))
  end

  -- 交给游戏自己的账号处理：fpxaccount.lua M.onSdkLogin(bInitGame, result)
  local ok2, err = pcall(function()
    FPXAccount.onSdkLogin(pc.initialized, result)
  end)
  if not ok2 then
    LOG('deliver: onSdkLogin failed: ' .. tostring(err))
    return
  end

  LOG('account: sdk login delivered account_id='
    .. tostring(result.data and result.data.account_id))

  -- 与官服 fpxsdkreceiver.lua 第 17-20 行同样的进入判据
  if not pc.initialized and not pc.entered then
    pc.entered = true
    EnterGame()
    LOG('account: EnterGame invoked')
  end
end

-- ------------------------------------------------------------
-- 登录入口：被 platform/login 拦截后走到这里
-- ------------------------------------------------------------
local function login()
  if pc.pending then
    LOG('login: already pending, ignored')
    return
  end

  pcall(probe_dump, 'at-login')

  local status = OO_Internal_RequestLogin()

  if status ~= 'floating' then
    return deliver(OO_Internal_Result())
  end

  pc.pending = true
  local tick = GameTick

  rawset(_G, 'GameTick', function(...)
    local raw = OO_Internal_Result()
    if raw ~= '' then
      pc.pending = false
      rawset(_G, 'GameTick', tick)
      CS.LuaHelper.BindGameTickFunc()
      deliver(raw)
      return
    end
    if tick then return tick(...) end
  end)

  CS.LuaHelper.BindGameTickFunc()
  LOG('login: floating, waiting for result on GameTick')
end

-- ------------------------------------------------------------
-- 全局回调包装（安全）
-- ------------------------------------------------------------
-- 这些是普通全局函数，不是类方法，包装它们不会触发游戏的
-- "class X has repeat attr Y" 校验。
--
-- 目的：确认原生层到底有没有回调 RpcHandlerCallbackOnConnect。
-- 如果它从不被调用，cObj 就永远不会被 setCobj 赋值，
-- 也就解释了 "dispatchRpc ... rpcHandler is invalid"。
-- ------------------------------------------------------------
-- 实体类名增量注册（必须早于 install_cb_wrappers 定义）
-- ------------------------------------------------------------
-- ★ 注意两点：
--   1. 绝对不能用 original_require 主动加载 EntityFactory ——
--      那会让它在依赖就绪前被强制加载，游戏卡死在主界面加载动画。
--      只从 package.loaded 里读。
--   2. 不能只注册一次。EntityFactory._entities 是 parseEntity 逐条填的，
--      第一次抓到的时候可能只有 1 条（实测就是 1 条），
--      所以要**增量、可重入**，每次把新出现的名字补上。
local ENT_REGISTERED = {}

local function register_entity_names()
  local EF = package.loaded['Core.Common.EntityFactory']
  if type(EF) ~= 'table' then return end
  local ents = EF._entities
  if type(ents) ~= 'table' then return end
  local ph = package.loaded['phonestcore']
  if type(ph) ~= 'table' or type(ph.registerRpc) ~= 'function' then return end
  local n = 0
  for name in pairs(ents) do
    if type(name) == 'string' and not ENT_REGISTERED[name] then
      ENT_REGISTERED[name] = true
      pcall(ph.registerRpc, name)
      n = n + 1
    end
  end
  if n > 0 then
    LOG('entreg: 新增实体类名 ' .. n .. ' 个')
    local ok2, idx = pcall(ph.genRpcIndex, 'ClientAvatar')
    if ok2 then
      local ok3, back = pcall(ph.getRpcName, idx)
      LOG('entreg: ClientAvatar -> ' .. tostring(idx) .. ' 反查=' .. tostring(back))
    end
  end
end

-- ------------------------------------------------------------
-- 把 Lua 日志级别提到 DEBUG
-- ------------------------------------------------------------
-- 依据 game.lua:299-305：
--     if GlobalConst.DEBUG_MODE == true then
--       LoggerManager.setLevel(loggingCls.DEBUG)
--     else
--       LoggerManager.setLevel(loggingCls.WARN)     -- 正式包走这里
--       LoggerManager.setEngineLogLevel(loggingCls.ERROR)
--     end
--
-- 正式包里 Lua 侧是 WARN，所有 logger:info(...) 全被吞掉。
-- 这导致我们一直看不到客户端到底走到哪一步（比如
-- AvatarTimeComp 的 "Client-Server Ping" 就是 logger:info）。
-- 这里把它提回 DEBUG。不改游戏源码，只在运行时调它的公开 API。
local function bump_lua_log_level()
  if pc.loglv then return end
  local LM = package.loaded['Core.Log.LoggerManager']
  if type(LM) ~= 'table' then return end
  local ok1, loggingCls = pcall(LM.getLoggingClass)
  if not ok1 or type(loggingCls) ~= 'table' then return end
  local ok2 = pcall(LM.setLevel, loggingCls.DEBUG)
  if ok2 then
    pc.loglv = true
    LOG('loglv: Lua 日志级别已提到 DEBUG')
  end
end

-- ------------------------------------------------------------
-- 打开 TableDump 输出
-- ------------------------------------------------------------
-- ★ 这是登录失败的真正原因（2026-10-19 实机日志）：
--     [GLog] exception got: ClientAvatar:init, TableDump is disabled.
--            Open by: GlobalConst.EnableTableDumpOutput
--     [GLog] [Login] login failed, got exception.
--
-- game.lua:286 里：
--     GlobalConst.EnableTableDumpOutput = GlobalConst.DEBUG_MODE
-- 正式包 DEBUG_MODE = false，于是 ClientAvatar:init 里调 TableDump 直接抛异常，
-- 整个 init 中断 -> 登录失败 -> 回 LoadingPanel -> 重连循环。
--
-- 这不是改游戏源码，只是把它自己的公开开关打开。
local function enable_table_dump()
  if pc.tabledump then return end
  local GC = package.loaded['GlobalConst']
  if type(GC) ~= 'table' then return end
  GC.EnableTableDumpOutput = true
  local ok = pcall(function()
    local LU = package.loaded['Utils.LogUtils']
    if type(LU) == 'table' then LU.EnableTablePrint = true end
  end)
  pc.tabledump = true
  LOG('tabledump: EnableTableDumpOutput = true' .. (ok and '' or ' (LogUtils 跳过)'))
end

local CB_NAMES = {
  'RpcHandlerCallbackOnConnect',
  'RpcHandlerCallbackOnClose',
  'RpcHandlerCallbackOnMethod',
  'RpcHandlerCallbackProtoExceedLimit',
  'CallbackManagerHandler',
}

-- ------------------------------------------------------------
-- 探测 C# 资源 API：场景名 -> 资源包 到底怎么解析
-- ------------------------------------------------------------
-- 背景（实机日志）：
--   ReportException: UnityLogError No Asset Bundle found for Scene: sc_mainstage
-- 这句话出自 C#（Core.ResourceManager / DeepClient.SceneManagement），
-- Assembly-CSharp.dll 是加密的（文件头 cb9422e1，不是 MZ），没法直接反编译。
-- 但 xLua 把 C# 类型暴露给 Lua，可以用 .NET 反射把 Core.ResourceManager 和
-- DeepAssetBundle 的公开方法列出来，再挑「名字 -> 包」的那个直接调，
-- 问它 sc_mainstage 在不在。这比猜加密清单的密钥实在。
-- ------------------------------------------------------------
-- 街区 Actor 探针：进街区后问客户端自己「人在不在、模型有没有」
-- ------------------------------------------------------------
-- 依据 stage/loadstep/impl/loadstepstreet.lua:109-165：
--   curCharId = StreetPlayerRoleMgr:getCurRoleId(streetId)
--   if data[curCharId] then
--     skin = GlobalData.Avatar:getUsingSkinId(curCharId)
--     modelPath = CharAppearanceData[skin].modelStreet
--     player:loadActorView(modelPath, cb)
--   else 直接结束（人不在）
-- 所以这里把这条链上的每一环都打出来，看断在哪。
-- ------------------------------------------------------------
-- 阶段 1 实机验收探针
-- ------------------------------------------------------------
-- 目标：不用肉眼看，直接从运行中的客户端读三件事：
--   1-1 characterBag（角色）、1-2 item_bag（背包货币）、1-3 玩家数据；
--   2) StreetV2Panel / StreetHpPanel 是否真的打开；
--   3) HUD 关键控件（疾跑按钮、编队格子）是否绑定成功。
-- ------------------------------------------------------------
-- 1-3 端到端验证：从客户端调 Set*_CS，验证「发出去 -> 服务端 ACK -> 回调执行」
-- ------------------------------------------------------------
-- 依据 entities/avatarcomponent/avataruserdatacomp.lua:238-334：
--   SetSignature_CS(signature, callback) -> serverRpc(..., CallbackHandler(self, "SetSignature_SC"))
--   SetSignature_SC(callback, retStatus, response) -> callback(retStatus)
-- 所以回调能拿到 status=true，就说明整条 RPC 链闭环了。
-- ------------------------------------------------------------
-- dump phonestcore 的全部 key（它暴露了 HandlerTypeGateClient 这类常量，
-- 也许还有消息类型枚举，能直接解掉 entityMessageResponse 的帧类型问题）
-- ------------------------------------------------------------
-- ------------------------------------------------------------
-- 让客户端自己编码一帧 entityMessageResponse，我们抓包看真实格式
-- ------------------------------------------------------------
-- core/net/rpchandler.lua:20-26 dispatchRpc 是双向的：
--   function RpcHandler:dispatchRpc(rpcName, ...)
--     if self.cObj then self.cObj:dispatchRpc(rpcName, ...) end
-- 所以客户端可以直接让它编码任意一个 handler 方法。
-- 我们调 entityMessageResponse(rid, parameters)，抓包就能拿到：
--   1) 帧头里的 u16 方法索引（= 真正的帧类型）
--   2) protobuf 字段布局（rid / parameters 各在几号字段）
-- ------------------------------------------------------------
-- 1-3 主数据通道验证：强制调一次 BatchGetUserData
-- ------------------------------------------------------------
-- 观察 signature 是否被服务端推的属性同步改掉
-- 1-2 使用道具验证：扣数量 + 属性同步
-- 1-1 角色升级验证
-- 系统入口解锁验证
local function probe_sysunlock()
  if pc.sysunlock then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    pc.sysunlock = true
    LOG('sysunlock| system_p = ' .. type(A.system_p))
    pcall(function()
      local n = 0
      for _ in pairs(A.system_p.unlocked_system) do n = n + 1 end
      LOG('sysunlock| unlocked_system 条目数 = ' .. n)
    end)
    LOG('sysunlock| level = ' .. tostring(A.level))
    LOG('sysunlock| TaskState.Finished = ' .. tostring(GlobalConst.TaskState.Finished))
    local okA, va = pcall(function() return A:getSystemTaskState(1) end)
    LOG('sysunlock| getSystemTaskState(1) ok=' .. tostring(okA) .. ' -> ' .. tostring(va))
    local okB, vb = pcall(function() return A:getTaskStateByTaskID(1005) end)
    LOG('sysunlock| getTaskStateByTaskID(1005) ok=' .. tostring(okB) .. ' -> ' .. tostring(vb))
    local okC, vc = pcall(function() return A:getSystemTaskState(15) end)
    LOG('sysunlock| getSystemTaskState(15) ok=' .. tostring(okC) .. ' -> ' .. tostring(vc))
    local items = { {'PlayerInfo', 1}, {'Role', 4}, {'Bag', 7}, {'Map', 11},
                    {'Formation', 30}, {'BattlePass', 32}, {'Menu', 1001} }
    for _, it in ipairs(items) do
      local ok, v = pcall(function() return A:isSysEntryUnlock(it[2]) end)
      LOG('sysunlock| isSysEntryUnlock(' .. it[1] .. '=' .. it[2] .. ') ok='
        .. tostring(ok) .. ' -> ' .. tostring(v))
    end
  end)
end
local function probe_upgrade()
  if pc.upgradeprobe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil or A:isServerLost() then return end
    pc.upgradeprobe = true
    local lv = 'nil'
    pcall(function() lv = tostring(A:getCharacterLevel(9001)) end)
    LOG('upgrade| before getCharacterLevel(9001)=' .. lv)
    A:UpgradeCharacter(9001, 1, {})
  end)
end
local function probe_useitem()
  if pc.useitemprobe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil or A:isServerLost() then return end
    pc.useitemprobe = true
    local before = 'nil'
    pcall(function() before = tostring(A:getItemNumAll(101)) end)
    LOG('useitem| before getItemNumAll(101)=' .. before)
    pcall(function()
      LOG('useitem| item_bag type=' .. type(A.item_bag)
        .. ' _id=' .. tostring(A.item_bag._id)
        .. ' __id__=' .. tostring(A.item_bag.__id__))
      local cache = A.__CustomPropertiesCache__
      LOG('useitem| CustomPropertiesCache type=' .. type(cache))
      if type(cache) == 'table' then
        local ids = {}
        for k, v in pairs(cache) do ids[#ids + 1] = tostring(k) end
        table.sort(ids)
        LOG('useitem| cache ids = ' .. table.concat(ids, ','))
      end
    end)
    A:UseItem_CS(101, 100, nil, function(status, response)
      LOG('useitem| 回调 status=' .. tostring(status and status.status))
      local after = 'nil'
      pcall(function() after = tostring(A:getItemNumAll(101)) end)
      LOG('useitem| after  getItemNumAll(101)=' .. after)
    end)
  end)
end
local function probe_sig_watch()
  pc.sigWatch = (pc.sigWatch or 0) + 1
  if pc.sigWatch > 600 then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    local cur = tostring(A.signature)
    -- 值变化立刻打，另外每 10 次打一条心跳式日志
    if pc.sigLast ~= cur or (pc.sigWatch % 10) == 0 then
      LOG('sigwatch| #' .. pc.sigWatch .. ' signature=[' .. cur .. ']')
      pc.sigLast = cur
    end
    -- 道具数量也一起盯：属性同步帧排在应答帧后面，
    -- 应答回调里读到的还是旧值，必须在这里持续观察。
    local n110 = 'nil'
    pcall(function() n110 = tostring(A:getItemNumAll(110)) end)
    if pc.n110Last ~= n110 then
      LOG('itemwatch| #' .. pc.sigWatch .. ' 时砂之盒(110)=' .. n110)
      pc.n110Last = n110
    end
    local cnt = 'nil'
    pcall(function() cnt = tostring(A:getItemNumAll(101)) end)
    if pc.itemLast ~= cnt then
      LOG('itemwatch| #' .. pc.sigWatch .. ' item101=' .. cnt)
      pc.itemLast = cnt
    end
    -- 角色等级也盯一下
    local lv = 'nil'
    pcall(function() lv = tostring(A:getCharacterLevel(9001)) end)
    if pc.lvLast ~= lv then
      LOG('lvwatch| #' .. pc.sigWatch .. ' charLevel9001=' .. lv)
      pc.lvLast = lv
    end
    -- 1-3 的 setter 目标属性
    local pv = tostring(A.portrait_id)
    if pc.pvLast ~= pv then
      LOG('setwatch| #' .. pc.sigWatch .. ' portrait_id=' .. pv)
      pc.pvLast = pv
    end
    local cs = tostring(A.is_char_list_shown)
    if pc.csLast ~= cs then
      LOG('setwatch| #' .. pc.sigWatch .. ' is_char_list_shown=' .. cs)
      pc.csLast = cs
    end
  end)
end
local function probe_userdata()
  if pc.udprobe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    if A:isServerLost() then return end
    pc.udprobe = true
    LOG('udprobe| 调 BatchGetUserData，uid=' .. tostring(A.uid))
    A:BatchGetUserData({ A.uid }, function(status, response)
      LOG('udprobe| 回调 status=' .. tostring(status and status.status))
      if response and response.Results then
        for _, info in ipairs(response.Results) do
          LOG('udprobe|   Uid=' .. tostring(info.Uid))
          local n = 0
          for k, v in pairs(info.AttributesMap or {}) do
            n = n + 1
            if n <= 12 then
              LOG('udprobe|     ' .. tostring(k) .. ' = ' .. tostring(v))
            end
          end
          LOG('udprobe|     属性数=' .. n)
        end
      else
        LOG('udprobe|   response.Results 为空')
      end
    end)
  end)
end
local function probe_encode_response()
  if pc.encresp then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local CR = original_require('Core.Client.ClientRepo')
    local nh = CR.netHandler
    if nh == nil then return end
    local gc = nh.gateClient
    if gc == nil then return end
    local h = gc.handler
    if h == nil or h.cObj == nil then return end
    pc.encresp = true
    -- msgpack: [ {status:true, errmsg:""}, {} ]
    local payload = string.char(0x92)
      .. string.char(0x82) .. string.char(0xA6) .. 'status' .. string.char(0xC3)
      .. string.char(0xA6) .. 'errmsg' .. string.char(0xA0)
      .. string.char(0x80)
    LOG('encresp| payload hex = ' .. string.format('%s', (payload:gsub('.', function(c) return string.format('%02x', string.byte(c)) end))))
    LOG('encresp| 调 cObj:dispatchRpc(entityMessageResponse, 4242, payload)')
    local ok, err = pcall(function() h.cObj:dispatchRpc('entityMessageResponse', 4242, payload) end)
    LOG('encresp| ok=' .. tostring(ok) .. ' err=' .. tostring(err))
  end)
end
local function probe_phonestcore_keys()
  if pc.phkeys then return end
  pc.phkeys = true
  local ok, ph = pcall(original_require, 'phonestcore')
  if not ok or type(ph) ~= 'table' then
    LOG('phkeys: phonestcore 不可用')
    return
  end
  local keys = {}
  for k, v in pairs(ph) do
    keys[#keys + 1] = tostring(k) .. ' = ' .. tostring(v)
  end
  table.sort(keys)
  LOG('phkeys| phonestcore 共 ' .. #keys .. ' 个 key')
  for i = 1, math.min(#keys, 120) do
    LOG('phkeys| ' .. keys[i])
  end
end
-- 探针开关：需要跑 1-3 端到端 RPC 验证时置 true
OO_PROBE_RPC = false    -- Set* 系列端到端探针（会改数据，默认关）
OO_PROBE_WRITE = false  -- UseItem / UpgradeCharacter 写操作探针（会改数据）

local function probe_p1_setrpc()
  if pc.setrpc then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    -- ★ 先看 server 在不在：isServerLost() 就是 self.server == nil（cliententity.lua:35-37）
    local lost = true
    pcall(function() lost = A:isServerLost() end)
    if lost then
      -- 还没绑上就下次再试，别把探测标记置上
      pc.setrpcTries = (pc.setrpcTries or 0) + 1
      if pc.setrpcTries <= 3 or (pc.setrpcTries % 20) == 0 then
        LOG('setrpc| server 未就绪（isServerLost=true），第 ' .. pc.setrpcTries .. ' 次重试')
      end
      return
    end
    LOG('setrpc| === 1-3 端到端 RPC 验证（server 已就绪）===')
    local function cb(tag)
      return function(ret)
        local st = 'nil'
        if ret ~= nil then st = tostring(ret.status) end
        LOG('setrpc| ' .. tag .. ' 回调 status=' .. st)
      end
    end
    -- 一次发 5 条，rid 连续；服务端按 rid % 5 回不同的 (帧类型, 布局) 组合。
    -- 哪个回调拿到 status=true，就说明那组格式是对的。
    local calls = {
      { 'SetSignature',              function(c) A:SetSignature_CS('OpenOrange', c) end },
      { 'SetIsCharListShown',        function(c) A:SetIsCharListShown_CS(1, c) end },
      { 'SetPortrait',               function(c) A:SetPortrait_CS(12345, c) end },
      { 'SetIsHonorWallShown',       function(c) A:SetIsHonorWallShown_CS(1, c) end },
      { 'SetIsAccountCreateTimeHidden', function(c) A:SetIsAccountCreateTimeHidden_CS(0, c) end },
    }
    -- 5 个 setter 各调 4 次 = 20 次，rid 连续 20 个 -> rid %% 20 覆盖全部 20 组组合
    -- ★ 实验：改成「每次调用只发一条」，间隔一个 tick（约 0.5 秒），
    --   区分是「突发被原生层丢」还是「发到第 N 条就停」。
    local CR = original_require('Core.Client.ClientRepo')
    pc.setrpcSent = pc.setrpcSent or 0
    pc.setrpcIdx = pc.setrpcIdx or 1
    local i = pc.setrpcIdx
    local item = calls[i]
    pc.setrpcIdx = (i % #calls) + 1
    pc.setrpcSent = pc.setrpcSent + 1
    local n = pc.setrpcSent
    local srv = A.server
    local stub = srv and srv.stub
    local cobj = stub and stub.cObj
    local nr = 'nil'
    pcall(function() nr = tostring(CR.responseManager.nextRid) end)
    LOG('setrpc| #' .. n .. ' ' .. item[1] .. ' 前: cObj=' .. type(cobj) .. ' nextRid=' .. nr)
    local ok, e = pcall(function() item[2](cb(item[1] .. '#' .. n)) end)
    if not ok then
      LOG('setrpc| ' .. item[1] .. ' 发出失败: ' .. tostring(e))
    end
    local nr2 = 'nil'
    pcall(function() nr2 = tostring(CR.responseManager.nextRid) end)
    LOG('setrpc| #' .. n .. ' 后: nextRid=' .. nr2)
    if n >= 20 then
      pc.setrpc = true
      LOG('setrpc| 20 条已发完（每条间隔一个 tick）')
    end
  end)
end
local function probe_p1_accept()
  if pc.p1probe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    pc.p1probe = true
    LOG('p1| === 阶段 1 实机验收 ===')
    local A = GlobalData.Avatar
    LOG('p1| Avatar = ' .. type(A))
    if A == nil then return end
    local okc, cnt = pcall(function()
      local c = 0
      for _ in pairs(A.characterBag) do c = c + 1 end
      return c
    end)
    LOG('p1| characterBag 条目数 ok=' .. tostring(okc) .. ' n=' .. tostring(cnt))
    local ok2, cb = pcall(function() return A.characterBag[9001] end)
    LOG('p1| characterBag[9001] = ' .. type(cb))
    if cb ~= nil then
      LOG('p1|   useSkinId = ' .. tostring(cb.useSkinId))
      LOG('p1|   skins = ' .. type(cb.skins))
      LOG('p1|   biography_read = ' .. tostring(cb.biography_read))
    end
    local oki, n = pcall(function()
      local c = 0
      for _ in pairs(A.item_bag) do c = c + 1 end
      return c
    end)
    LOG('p1| item_bag 条目数 ok=' .. tostring(oki) .. ' n=' .. tostring(n))
    local ok4, n101 = pcall(function() return A:getItemNumAll(101) end)
    LOG('p1| getItemNumAll(101) ok=' .. tostring(ok4) .. ' v=' .. tostring(n101))
    local ok5, n103 = pcall(function() return A:getItemNumAll(103) end)
    LOG('p1| getItemNumAll(103) ok=' .. tostring(ok5) .. ' v=' .. tostring(n103))
    for _, k in ipairs({ 'player_name', 'gender', 'portrait_id', 'signature',
                         'street_id', 'level', 'uid' }) do
      LOG('p1| A.' .. k .. ' = ' .. tostring(A[k]))
    end
    local pm = ui.GetPanel(PanelNames.StreetV2Panel)
    LOG('p1| StreetV2Panel = ' .. type(pm))
    if type(pm) == 'table' then
      LOG('p1|   _btn_run_pc = ' .. type(pm._btn_run_pc))
      LOG('p1|   _node_teammenbers_v2 = ' .. type(pm._node_teammenbers_v2))
      LOG('p1|   _grid_teammenbers_pc = ' .. type(pm._grid_teammenbers_pc))
      if type(pm._member_map) == 'table' then
        local c = 0
        for _ in pairs(pm._member_map) do c = c + 1 end
        LOG('p1|   _member_map 条目 = ' .. c)
      else
        LOG('p1|   _member_map = ' .. type(pm._member_map))
      end
    end
    LOG('p1| StreetHpPanel = ' .. type(ui.GetPanel(PanelNames.StreetHpPanel)))
  end)
end
local function probe_street_actor()
  if pc.streetprobe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil then return end
    local nm = stage.name
    if nm ~= GlobalConst.EGameStage.Street then return end
    pc.streetprobe = true
    LOG('street| === 街区 Actor 探测 ===')
    local A = GlobalData.Avatar
    LOG('street| Avatar = ' .. type(A))
    if A == nil then return end
    local cid = A.character_choosen_char
    LOG('street| character_choosen_char = ' .. tostring(cid))
    local skin
    local okSkin, s = pcall(function() return A:getUsingSkinId(cid) end)
    if okSkin then skin = s end
    LOG('street| getUsingSkinId(' .. tostring(cid) .. ') = ' .. tostring(skin))
    local app = DataTable('Common', 'CharAppearanceData')
    local appd = app and skin and app[skin]
    LOG('street| CharAppearanceData[' .. tostring(skin) .. '] = ' .. type(appd))
    if appd then
      LOG('street|   modelStreet = ' .. tostring(appd.modelStreet))
    end
    local logic = game.stageMgr:getStreetStageLogic()
    LOG('street| streetStageLogic = ' .. type(logic))
    if logic then
      local okMP, mp = pcall(function() return logic:getMainPlayer() end)
      LOG('street| getMainPlayer() ok=' .. tostring(okMP) .. ' -> ' .. type(mp))
      if okMP and mp then
        LOG('street|   charId = ' .. tostring(mp.charId))
        LOG('street|   viewAssetPath = ' .. tostring(mp.viewAssetPath))
        LOG('street|   actorModel = ' .. type(mp.actorModel))
        if mp.actorModel then
          LOG('street|   actorModel.actorView = ' .. type(mp.actorModel.actorView))
          pcall(function()
            local go = mp.actorModel.actorModel
            LOG('street|   actorModel.actorModel = ' .. type(go))
            if go ~= nil and not LuaHelper.IsNull(go) then
              LOG('street|     name = ' .. tostring(go.name))
              LOG('street|     activeSelf = ' .. tostring(go.activeSelf))
              LOG('street|     activeInHierarchy = ' .. tostring(go.activeInHierarchy))
              local tr = go.transform
              if tr ~= nil and not LuaHelper.IsNull(tr) then
                local p = tr.position
                LOG('street|     pos = ' .. tostring(p.x) .. ',' .. tostring(p.y) .. ',' .. tostring(p.z))
              end
              local kids = go.transform.childCount
              LOG('street|     childCount = ' .. tostring(kids))
            else
              LOG('street|     actorModel.actorModel 为 nil 或已销毁')
            end
          end)
          -- 摄像机到底跟没跟：直接比摄像机位置和玩家位置
          pcall(function()
            local cam = CS.UnityEngine.Camera.main
            if cam == nil then LOG('street|   Camera.main = nil'); return end
            local cp = cam.transform.position
            LOG('street|   camera pos = ' .. tostring(cp.x) .. ',' .. tostring(cp.y) .. ',' .. tostring(cp.z))
            local pp = mp.actorModel.actorModel.transform.position
            local dx = cp.x - pp.x
            local dy = cp.y - pp.y
            local dz = cp.z - pp.z
            local d = math.sqrt(dx*dx + dy*dy + dz*dz)
            LOG('street|   camera 到玩家距离 = ' .. string.format('%.2f', d))
          end)
          -- 逐条调用 StreetV2Panel:setData -> refreshRoleRedPoint 链上的函数，
          -- 看哪一个抛异常
          pcall(function()
            local A = GlobalData.Avatar
            local names = { 'hasAnyCharacterCanRisingStar',
                            'getCharInFormationHasAnyNotFetchedCharBreakReward',
                            'isFullFormation' }
            for _, n in ipairs(names) do
              local ok, r = pcall(function() return A[n](A) end)
              LOG('panelprobe| ' .. n .. ' ok=' .. tostring(ok) .. ' r=' .. tostring(r))
            end
            local ok2, r2 = pcall(function() return A:checkAvatarFashionEquipRedPoint(false) end)
            LOG('panelprobe| checkAvatarFashionEquipRedPoint ok=' .. tostring(ok2) .. ' r=' .. tostring(r2))
            LOG('panelprobe| type(A.chars) = ' .. type(A.chars))
            -- 背包内容 + 直接调 onItemNumChanged，看是不是我们预置的条目有问题
            local n = 0
            for k, v in pairs(A.item_bag) do
              n = n + 1
              LOG('bagprobe| key=' .. tostring(k) .. ' id=' .. tostring(v.id)
                .. ' itemId=' .. tostring(v.itemId) .. ' num=' .. tostring(v.num))
            end
            LOG('bagprobe| item_bag 条目数 = ' .. n)
            for _, iid in ipairs({ 101, 103 }) do
              local ok3, e3 = pcall(function() return A:onItemNumChanged(iid) end)
              LOG('bagprobe| onItemNumChanged(' .. iid .. ') ok=' .. tostring(ok3) .. ' err=' .. tostring(e3))
            end
          end)
        end
      end
    end
  end)
end

local function probe_cs_api()
  if pc.csapi then return end
  pc.csapi = true

  local function dump_methods(tag, typeObj)
    local ok, err = pcall(function()
      LOG('csapi| === ' .. tag .. ' ===')
      local ms = typeObj:GetMethods()
      local n = 0
      local seen = {}
      for i = 0, ms.Length - 1 do
        local m = ms[i]
        local nm = tostring(m.Name)
        if not seen[nm] then
          seen[nm] = true
          n = n + 1
          if n <= 160 then LOG('csapi| ' .. tag .. ' :: ' .. nm) end
        end
      end
      LOG('csapi| ' .. tag .. ' 方法数(去重)=' .. n)
    end)
    if not ok then LOG('csapi: ' .. tag .. ' 反射失败: ' .. tostring(err)) end
  end

  -- xLua 没包 System.Type.GetMethods（反射不可用），但 C# 成员可以直接调。
  -- 依据 modules/gmcommand/gmcommand.lua:88-120，资源管理器自己就有两个探针：
  --   Inst:DumpResourceInfo()            把资源表打出来
  --   Inst:IsAssetInBundle(assetName)    查某个资源在不在包里 -> bool
  --   Inst:IsAssetExist(assetPath)       查资源存不存在 -> bool
  -- 正好拿 login3（确定能加载）和 sc_mainstage（加载失败）做对照实验。
  local inst
  local okInst = pcall(function() inst = CS.Core.ResourceManager.Inst end)
  if not okInst or inst == nil then
    LOG('csapi: ResourceManager.Inst 拿不到')
    return
  end
  pcall(function()
    LOG('csapi| Inst 类型 = ' .. tostring(inst:GetType():ToString()))
  end)

  -- ★ 关键对照实验：
  --   IsAssetInBundle 到底是「全量包表」还是「已加载过的包」？
  --   login3 启动时就被加载过，所以它返回 true 不能证明包表完整。
  --   下面这些路径是从 tools/scan_bundles.py 扫出来的**真实容器路径**
  --   （work/oo_analysis/base_containers.json，673 个基础包里确实存在），
  --   但游戏从来没加载过它们。如果它们返回 true，说明包表是全量的，
  --   sc_mainstage 就是真的没有；如果返回 false，说明包表只记已加载的，
  --   那之前「sc_mainstage 不在包表里」的结论就不成立。
  local probes = {
    -- 对照组：启动时就加载过
    'login3', 'empty',
    -- 目标
    'sc_mainstage', 'scenes/sc_mainstage', 'Assets/res/scenes/sc_mainstage',
    -- 从未加载过的真实容器路径（一定在盘上）
    'env/mainstage/prop/env_mainstage_prop_light/p_env_mainstage_prop_light_02',
    'scenes/Street/zhp/sc_zhpmap_01/gvp_sc_zhp_restaurant_01',
    'scenes/Level/material/sc_material_6/p_lighting_sc_material_06',
    -- 从未加载过的场景名
    'sc_zhpmap_01', 'sc_material_6', 'sc_favorspace', 'sc_Infinite_challenge',
  }
  for _, nm in ipairs(probes) do
    local ok1, v1 = pcall(function() return inst:IsAssetInBundle(nm) end)
    local ok2, v2 = pcall(function() return inst:IsAssetExist(nm) end)
    LOG('csapi| probe ' .. nm .. ' | InBundle ok=' .. tostring(ok1) .. ' v=' .. tostring(v1)
      .. ' | Exist ok=' .. tostring(ok2) .. ' v=' .. tostring(v2))
  end

  -- 探测成员名：xLua 对不存在的成员会抛错，所以 pcall 成功就说明成员存在。
  -- 目标是找「资源名 -> 包名」的那一个方法。
  local members = {
    'GetBundleName', 'GetAssetBundleName', 'GetBundlePath', 'GetAssetPath',
    'GetBundleHash', 'GetBundleMd5', 'TryGetBundleName', 'GetAssetBundle',
    'HasBundle', 'ContainsBundle', 'GetDependencies', 'GetAssetBundleNames',
    'GetAllBundleNames', 'DumpResourceInfo', 'IsAssetInBundle', 'IsAssetExist',
    'LoadSceneAsync', 'LoadScene', 'Load', 'LoadAsync', 'GetBundleCount',
  }
  for _, nm in ipairs(members) do
    local ok, v = pcall(function() return inst[nm] end)
    LOG('csapi| member ' .. nm .. ' exists=' .. tostring(ok and v ~= nil))
  end

  -- ★★ 拿清单的解密密钥。
  -- 依据 patch/patchhelper.lua:305-318：
  --   PatchHelper.decodeJsonFileByAes(filePath)
  --     -> CSHelper.AesDecryptorBase64(encodedStr, CommonConfig.AesKey)
  -- 也就是说所有加密清单（patch_info.json / assets_md5_cache.json /
  -- patch_md5_cache.json / version.json）都是 AES+Base64，密钥是
  -- C# 的 CommonConfig.AesKey。把它读出来就能离线解密，不用再啃加密的
  -- Assembly-CSharp.dll。
  pcall(function()
    local CC = CS.UnityEngine.Utility.Funplus.Runtime.CommonConfig
    LOG('csapi| CommonConfig.AesKey = ' .. tostring(CC.AesKey))
  end)
  pcall(function()
    local PC = CS.Core.Patch.PatchConfig
    LOG('csapi| PatchConfig.AesKey = ' .. tostring(PC.AesKey))
  end)

  -- 顺手用游戏自己的函数解一份 patch_info.json，验证密钥对不对
  pcall(function()
    local PH = original_require('Patch.PatchHelper')
    local p = 'F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/PersistentPath/HybridData/patch_info.json'
    local t = PH.decodeJsonFileByAes(p)
    if type(t) == 'table' then
      local n = 0
      for _ in pairs(t) do n = n + 1 end
      LOG('csapi| patch_info 解密成功，顶层字段数=' .. n)
      local i = 0
      for k, v in pairs(t) do
        LOG('csapi|   patch_info.' .. tostring(k) .. ' = ' .. tostring(v))
        i = i + 1
        if i >= 12 then break end
      end
    else
      LOG('csapi| patch_info 解密失败 type=' .. type(t))
    end
  end)

  -- ★ 问 Unity 自己要内置场景列表。
  -- 如果 sc_mainstage 在内置列表里，那它是「构建进包里的场景」，
  -- No Asset Bundle found 那句话就是别的原因；
  -- 不在，才说明它是资源包场景。这是最直接的一问，之前一直没问。
  pcall(function()
    local SM = CS.UnityEngine.SceneManagement.SceneManager
    local SU = CS.UnityEngine.SceneManagement.SceneUtility
    local n = SM.sceneCountInBuildSettings
    LOG('csapi| 内置场景数 = ' .. tostring(n))
    for i = 0, math.min(n - 1, 200) do
      local okP, p = pcall(function() return SU.GetScenePathByBuildIndex(i) end)
      if okP then LOG('csapi|   buildScene[' .. i .. '] = ' .. tostring(p)) end
    end
  end)

  -- 再拿几个候选名直接问 IsAssetExist，看大厅到底叫什么
  pcall(function()
    for _, nm in ipairs({ 'sc_mainstage', 'mainstage', 'sc_hall', 'hall',
                          'sc_zhujiemian', 'sc_zhujiemianchengshi',
                          'sc_maincity', 'sc_street_main', 'empty', 'login3' }) do
      local ok, v = pcall(function() return inst:IsAssetExist(nm) end)
      LOG('csapi| exist2 ' .. nm .. ' = ' .. tostring(ok and v))
    end
  end)

  -- 把资源表整体打出来（游戏自己的 GM 功能，只读）
  local okDump, errDump = pcall(function() inst:DumpResourceInfo() end)
  LOG('csapi| DumpResourceInfo ok=' .. tostring(okDump) .. ' err=' .. tostring(errDump))
end
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
        -- ★ 只有 --auto 才接管登录（自动进入游戏）。
        --   不加 --auto 时走游戏原来的路径，会停在它自己的登录界面。
        if rawget(_G, 'OO_AUTO') == false then
          LOG('login: OO_AUTO=false，不接管 platform/login（停在游戏登录界面）')
          return call(module, fn, payload)
        end
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