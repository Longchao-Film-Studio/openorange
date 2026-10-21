-- oo/05a..05c_probe.lua —— 实机验收探针（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- 分两类，别搞混：
--
--   ★ 默认常开（只读）：probe_street_actor / probe_cs_api / probe_charbag /
--     probe_story / probe_sysunlock / probe_userdata / probe_sig_watch /
--     probe_phonestcore_keys / probe_p1_accept / probe_encode_response
--     它们只读状态、打日志，是「不看屏幕也能验收」的眼睛 —— 实机验证全靠它们
--     输出的 storyprobe / bagprobe / street| 这些行。改数据的不会混进来。
--
--   ★ 默认关闭（会改数据 / 发 RPC / 弹面板）：由 OO_PROBE_* 开关控制
--     （OO_PROBE_RPC / WRITE / STREET / STORY_ADV / STARTGAME / MAP ...）。
--     排障时才临时打开，生产链路绝不能默认开。
--
-- ⚠ 已知无害噪音：probe_encode_response 会一次性调
--     cObj:dispatchRpc('entityMessageResponse', 4242, payload)
--   客户端 ProtobufHandler 会打一行
--     [error] dispatchRpc but got wrong name entityMessageResponse
--   这是探针故意喂的假应答（用来确认应答帧的真实格式），不是 bug。

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
-- 地图面板验证：等价于按 M
-- 角色背包验证
-- 剧情任务 / 街景 NPC 验证
-- 街区切换实机验证：走客户端真实路径 AvatarStreet_SwitchStreet
local function probe_street_switch()
  if pc.streetswitched then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    if A:getStreetID() ~= 101 then return end
    -- ★ 必须等 server 代理就绪：gateclientrpchandler.lua:316-319 在断连时
    --   会 serverProxy:destroy()，那会把 owner.server 清成 nil；
    --   而 ClientEntity:serverRpc 在 isServerLost() 为 true 时**静默 return**，
    --   所以 A.server == nil 时发 RPC 是无声失败（实测踩过）。
    if A.server == nil or A:isServerLost() then
      return   -- 下一轮再试
    end
    pc.streetswitched = true
    local before = A:getStreetID()
    local sd0 = DataTable('Common', 'StreetData')[before]
    LOG('streetprobe| 切换前 street_id=' .. tostring(before)
      .. ' 街区=' .. tostring(sd0 and sd0.blockName))
    -- 注意：不能走 AvatarStreet_SwitchStreet —— 它里面先过 DLC 检查，
    -- 火锅店(1011)的资源没下载时会走 JumpToDlcStage，根本不发 RPC。
    -- 这里直接发同一条 RPC，验证服务端 + 属性同步 + 小地图。
    -- 诊断：serverRpc 里是 `if self.stub then dispatchRpc(...) end`，
    -- stub 为 nil 时静默不发 —— 必须把这几个值打出来。
    LOG('streetprobe| isServerLost=' .. tostring(A:isServerLost()))
    LOG('streetprobe| A.server=' .. tostring(A.server))
    if A.server then
      LOG('streetprobe| A.server.stub=' .. tostring(A.server.stub))
    end
    local okc, errc = pcall(function()
      A:serverRpc('RequestSwitchStreet_CS', {1011, nil})
    end)
    LOG('streetprobe| 发 RPC ok=' .. tostring(okc) .. ' err=' .. tostring(errc))
    TimerManager.addTimer(4, function()
      pcall(function()
        local now = A:getStreetID()
        local sd = DataTable('Common', 'StreetData')[now]
        LOG('streetprobe| 4秒后 street_id=' .. tostring(now)
          .. ' 街区=' .. tostring(sd and sd.blockName))
        local cur = game.stageMgr:getCurStage()
        LOG('streetprobe| 当前 stage=' .. tostring(cur and cur.name))
      end)
    end)
  end)
end
-- 剧情推进实机验证：模拟副本结算 Cmd_OfflineSpace_Finish_CS
-- 链条：StoryTask 1000 (TaskType=3, Parameter1=[1])
--   -> TaskData[1] "完成序章" -> TaskEventData[4001]
--   -> FinishDungeon{typeId:1004, modeId:1, level:1}
local function probe_story_advance()
  if pc.storyadvanced then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil or A.server == nil or A:isServerLost() then return end
    pc.storyadvanced = true
    LOG('storyprobe2| 结算前 1000=' .. tostring(A:getTaskStateByTaskID(1000))
      .. ' 1003=' .. tostring(A:getTaskStateByTaskID(1003)))
    local ok, err = pcall(function()
      A:serverRpc('Cmd_OfflineSpace_Finish_CS',
        {{typeId = 1004, modeId = 1, level = 1}})
    end)
    LOG('storyprobe2| 发结算 ok=' .. tostring(ok) .. ' err=' .. tostring(err))
    TimerManager.addTimer(3, function()
      pcall(function()
        LOG('storyprobe2| 结算后 1000=' .. tostring(A:getTaskStateByTaskID(1000))
          .. ' 1003=' .. tostring(A:getTaskStateByTaskID(1003)))
        local mgr = StreetSystemMgr.GetInstance()
        local n = 0
        if mgr and mgr._allActorByUniqueName then
          for _ in pairs(mgr._allActorByUniqueName) do n = n + 1 end
        end
        LOG('storyprobe2| 街上 actor 数=' .. tostring(n))
      end)
    end)
  end)
end

-- 剧情推进后街景 NPC 是否出现（验证 refreshActorState 动态刷新）
local function probe_story_npc()
  if pc.storynpc then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil or A.server == nil or A:isServerLost() then return end
    pc.storynpc = true
    -- StreetSystemMgr 是全局变量，由 stage/loadstep/loadstepbuilder.lua:116/119
    -- 在街区加载时赋值（Streaming / Normal 二选一），不是可 require 的模块。
    local mgr = nil
    if StreetSystemMgr ~= nil then
      local ok, r = pcall(function() return StreetSystemMgr.GetInstance() end)
      if ok then mgr = r end
    end
    if mgr == nil then
      local cands = {'System.StreetSystem.Manager.StreamingStreetSystemMgr',
                     'System.StreetSystem.Manager.NormalStreetSystemMgr'}
      for _, name in ipairs(cands) do
        local okm, mod = pcall(require, name)
        if okm and mod then
          local ok2, r2 = pcall(function() return mod.GetInstance() end)
          if ok2 and r2 then mgr = r2; break end
        end
      end
    end
    if mgr == nil then
      LOG('storyprobe3| 拿不到 StreetSystemMgr（全局=' .. tostring(StreetSystemMgr) .. '）')
      return
    end
    local function dump(tag)
      local names = {}
      if mgr._allActorByUniqueName then
        for k in pairs(mgr._allActorByUniqueName) do
          table.insert(names, tostring(k))
        end
      end
      table.sort(names)
      -- 关键：actor 对象总是创建，preTask 只控制 checkActorCondition() 的返回值
      -- （system/streetsystem/streetactorbase.lua:109-129）
      local condTxt = 'nil'
      pcall(function()
        local act = mgr:getActorByUniqueName('40004')
        if act ~= nil then
          local okc, cond = pcall(function() return act:checkActorCondition() end)
          local ad = act.actorData
          condTxt = tostring(cond) .. '/actorId=' .. tostring(ad and ad.actorId)
            .. '/preTask=' .. tostring(ad and ad.preTask and ad.preTask[1003])
        end
      end)
      LOG('storyprobe3| ' .. tag
        .. ' actor数=' .. tostring(#names)
        .. ' 1003=' .. tostring(A:getTaskStateByTaskID(1003))
        .. ' 40004可见条件=' .. condTxt)
    end
    dump('结算前')
    A:serverRpc('Cmd_OfflineSpace_Finish_CS',
      {{typeId = 1004, modeId = 1, level = 1}})
    TimerManager.addTimer(3, function()
      pcall(function() dump('结算后3秒') end)
    end)
  end)
end

-- 验证 Cmd_Avatar_StartGame_CS 的回调与应答
local function probe_startgame()
  if pc.sgcb then return end
  local A = GlobalData.Avatar
  if A == nil or A.server == nil or A:isServerLost() then return end
  pc.sgcb = true
  A.onProbeStartGame = function(self, ret, resp)
    LOG('sgprobe| 回调 ret.status=' .. tostring(ret and ret.status)
      .. ' resp非空=' .. tostring(resp ~= nil)
      .. ' spaceInfo=' .. tostring(resp and resp.spaceInfo ~= nil))
    if resp and resp.spaceInfo then
      local d = resp.spaceInfo
      LOG('sgprobe| data.spaceInfo=' .. tostring(d.spaceInfo ~= nil)
        .. ' copyId=' .. tostring(d.spaceInfo and d.spaceInfo.copyId)
        .. ' levelIndex=' .. tostring(d.spaceInfo and d.spaceInfo.levelIndex))
    end
  end
  local CH = require('Core.Common.CallbackHandler')
  A:serverRpc('Cmd_Avatar_StartGame_CS',
    {{match_type = 3, system_type = 1004, modeId = 1, storyID = 1}},
    CH(A, 'onProbeStartGame'))
  LOG('sgprobe| 已发 StartGame')
end

-- 直接测 enterOnlineHub，把内部被 xpcall 吞掉的错误暴露出来
local function probe_enter_hub()
  if pc.ehub then return end
  local A = GlobalData.Avatar
  if A == nil or A.server == nil or A:isServerLost() then return end
  pc.ehub = true
  local okr, SpaceUtils = pcall(require, 'Entities.SpaceEntities.SpaceUtils')
  if not okr or SpaceUtils == nil then
    LOG('ehub| require SpaceUtils 失败: ' .. tostring(SpaceUtils))
    return
  end
  local data = {spaceInfo = {copyId = 'guide_01', levelIndex = 1}}
  LOG('ehub| 开始 enterOnlineHub copyId=guide_01 levelIndex=1')
  local ok, err = pcall(function()
    SpaceUtils.enterOnlineHub(GlobalConst.EGameStage.OfflineBattle, data, true)
  end)
  LOG('ehub| enterOnlineHub ok=' .. tostring(ok) .. ' err=' .. tostring(err))
end

-- 逐步验证 hub:init / getCopy，定位 enterOfflineBattle 在哪一步停住
local function probe_hub_steps()
  if pc.hubsteps then return end
  local A = GlobalData.Avatar
  if A == nil or A.server == nil or A:isServerLost() then return end
  pc.hubsteps = true
  local ok, err = pcall(function()
    local HubOnline = require('BattleContainer.DispatchHub.DispatchHubOnline')
    local hub = HubOnline()
    local info = {copyId = 'guide_01', levelIndex = 1}
    -- 真实路径里 enterOnlineHub 会先调 fillSpaceInitInfo（spaceutils.lua:208），
    -- 补 userList/userTeamList/playerInfoDict/uid/spaceLevel/gameMode。
    -- 不补的话 hub:init 会在 DispatchHubBase:initProgress 里 pairs(nil) 报错。
    local SpaceUtils = require('Entities.SpaceEntities.SpaceUtils')
    SpaceUtils.fillSpaceInitInfo(info, 'guide_01')
    LOG('hubsteps| fillSpaceInitInfo 后 uid=' .. tostring(info.uid)
      .. ' spaceLevel=' .. tostring(info.spaceLevel)
      .. ' userList=' .. tostring(info.userList ~= nil))
    local okinit, retinit = pcall(function()
      return hub:init(GlobalConst.EGameStage.OfflineBattle, info, false)
    end)
    LOG('hubsteps| hub:init ok=' .. tostring(okinit) .. ' ret=' .. tostring(retinit))
    local copy = nil
    local okc, rc = pcall(function() return hub:getCopy(1) end)
    if okc then copy = rc end
    LOG('hubsteps| getCopy(1) ok=' .. tostring(okc) .. ' copy=' .. tostring(copy))
    if copy ~= nil then
      local ldi = copy.levelDataInfo
      LOG('hubsteps| levelDataInfo=' .. tostring(ldi ~= nil)
        .. ' levelId=' .. tostring(ldi and ldi.levelId)
        .. ' levelData=' .. tostring(ldi and ldi.levelData and ldi.levelData[1]))
      LOG('hubsteps| copy.battleInfo=' .. tostring(copy.battleInfo ~= nil))
    end
    LOG('hubsteps| hub.stage=' .. tostring(hub.stage))
  end)
  if not ok then LOG('hubsteps| 探针异常: ' .. tostring(err)) end
end

-- 观察 LoadingStage 的就绪标志，定位卡在哪个 flag
local function probe_loading_state()
  pcall(function()
    local st = game.stageMgr:getCurStage()
    if st == nil then return end
    if st.name ~= GlobalConst.EGameStage.Loading then return end
    if pc.lstatecnt == nil then pc.lstatecnt = 0 end
    pc.lstatecnt = pc.lstatecnt + 1
    if pc.lstatecnt > 20 then return end
    local flags = {}
    if st._readyFlagState then
      for k, v in pairs(st._readyFlagState) do
        table.insert(flags, tostring(k) .. '=' .. tostring(v))
      end
    end
    LOG('lstate|#' .. tostring(pc.lstatecnt)
      .. ' jump=' .. tostring(st.jump)
      .. ' flags=' .. table.concat(flags, ',')
      .. ' jumpStage=' .. tostring(st.data and st.data.jumpStage)
      .. ' levelName=' .. tostring(st.data and st.data.levelName)
      .. ' gpType=' .. tostring(st.gamePlayingType))
  end)
end

-- 看 LoadStepManager 当前卡在哪个加载步骤
local function probe_loadstep()
  pcall(function()
    local st = game.stageMgr:getCurStage()
    if st == nil or st.name ~= GlobalConst.EGameStage.Loading then return end
    if pc.lscnt == nil then pc.lscnt = 0 end
    pc.lscnt = pc.lscnt + 1
    if pc.lscnt > 15 then return end
    local LSM = require('Stage.LoadStep.LoadStepManager')
    local mgr = LSM.GetInstance()
    if mgr == nil then
      LOG('loadstep|#' .. tostring(pc.lscnt) .. ' LoadStepManager 为 nil')
      return
    end
    local name = 'nil'
    if mgr.curStep ~= nil then
      local ok, n = pcall(function()
        local ct = mgr.curStep.__ClassType
        return ct and ct.typeName or tostring(mgr.curStep)
      end)
      name = tostring(n)
    end
    LOG('loadstep|#' .. tostring(pc.lscnt)
      .. ' cur=' .. name
      .. ' progress=' .. tostring(mgr.curProgress))
    -- LoadStepUI 并行加载这 4 个面板，某个回调不回就永远卡住。
    -- PanelNames / battle 都是全局变量（loadstepbuilder.lua 里直接用了）。
    local bpm = 'ERR'
    pcall(function() bpm = tostring(battle.battleUiMgr.battlePanelName) end)
    local pn = 'ERR'
    pcall(function()
      pn = tostring(PanelNames.BattleHpPanel) .. '|'
        .. tostring(PanelNames.MessagePanel) .. '|'
        .. tostring(PanelNames.BlackPanel)
    end)
    LOG('loadstep|   battlePanelName=' .. bpm .. '  panels=' .. pn)
  end)
end

-- 给 ui.PreLoadPanelByName 打桩，记录每个面板的进入/回调
local function install_preload_hook()
  if pc.preloadhooked then return end
  if ui == nil or ui.PreLoadPanelByName == nil then return end
  pc.preloadhooked = true
  local orig = ui.PreLoadPanelByName
  ui.PreLoadPanelByName = function(name, cb)
    LOG('preload| enter ' .. tostring(name))
    local function wrapped(go)
      LOG('preload| done  ' .. tostring(name) .. ' go=' .. tostring(go ~= nil))
      if cb then return cb(go) end
    end
    local ok, err = pcall(function()
      return orig(name, wrapped)
    end)
    if not ok then
      LOG('preload| ERROR ' .. tostring(name) .. ' err=' .. tostring(err))
    end
  end
  LOG('preload| 钩子已安装')
end

-- 诊断用：面板预加载超时后放行
-- 背景：PanelManager:preLoadPanelByName（ui/base/panelmanager.lua:693-702）
--   加载失败时**不调回调**，导致 LoadStepUI 永远不完成。
-- 这里在运行时包一层：超过 N 秒没回调就手动 cb(nil) 放行，
-- 用来验证「除了这个面板资源，副本流程本身能不能跑通」。
-- 注意：这是诊断手段，不是修复 —— 缺的 HUD 面板依然缺。
local PANEL_TIMEOUT_SEC = 6
