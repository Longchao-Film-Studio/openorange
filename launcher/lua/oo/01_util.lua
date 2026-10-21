-- oo/01_util.lua —— 公共工具（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- LOG / safe / dump_map / argstr / probe_dump 这几个是所有模块都要用的地基。
-- 拆文件后仍然由 build.rs 按文件名顺序拼成**同一个 Lua chunk**，
-- 所以这里用 local function 定义、后面文件直接引用是安全的
-- （不是 require，各文件之间共享同一个作用域）。

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

