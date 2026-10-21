-- oo/02a_protocol.lua + 02b_protocol.lua —— 协议层（P0-E3 从 oo_bootstrap.lua 拆出）
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
