-- oo/04_login.lua —— 登录入口（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- platform/login 被拦截后走到 login()：拿启动器签发的登录结果、
-- 调原生 OO_Internal_RequestLogin、把结果交给 deliver() 落进客户端。

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
