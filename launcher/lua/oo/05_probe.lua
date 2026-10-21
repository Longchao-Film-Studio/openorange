-- oo/05_probe.lua —— 实机验收探针（P0-E3 从 oo_bootstrap.lua 拆出）
--
-- 这些探针都是**默认关闭**的（见文件末尾 OO_PROBE_* 开关），只在实机排查时
-- 临时打开。它们会改数据、弹面板、发 RPC，生产链路绝不能默认开。

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
local function install_panel_timeout()
  if pc.paneltimeout then return end
  if ui == nil or ui.PreLoadPanelByName == nil then return end
  pc.paneltimeout = true
  local orig = ui.PreLoadPanelByName
  ui.PreLoadPanelByName = function(name, cb)
    local done = false
    local function once(go)
      if done then return end
      done = true
      if cb then cb(go) end
    end
    orig(name, function(go)
      LOG('paneltmo| 正常返回 ' .. tostring(name))
      once(go)
    end)
    TimerManager.addTimer(PANEL_TIMEOUT_SEC, function()
      if not done then
        LOG('paneltmo| 超时放行 ' .. tostring(name)
          .. '（该面板资源加载失败，HUD 会缺失）')
        once(nil)
      end
    end)
  end
  LOG('paneltmo| 钩子已安装，超时 ' .. PANEL_TIMEOUT_SEC .. ' 秒')
end

-- 自动触发「进入下一关」，验证 Cmd_Avatar_EnterNext_CS 链路
local function probe_next_level()
  if pc.nextlv then return end
  pcall(function()
    local st = game.stageMgr:getCurStage()
    if st == nil or st.name ~= GlobalConst.EGameStage.OfflineBattle then return end
    local hp = GlobalData.hubPlayer
    if hp == nil then return end
    if pc.nextlvdone then return end
    pc.nextlv = true
    local ci = hp.nowCopyIndex
    LOG('nextlv| 当前 copyIndex=' .. tostring(ci))
    local ok, err = pcall(function()
      hp:enterNextBattleLogic()
    end)
    LOG('nextlv| enterNextBattleLogic ok=' .. tostring(ok) .. ' err=' .. tostring(err))
    TimerManager.addTimer(8, function()
      pcall(function()
        pc.nextlvdone = true
        local hp2 = GlobalData.hubPlayer
        LOG('nextlv| 8秒后 copyIndex=' .. tostring(hp2 and hp2.nowCopyIndex))
      end)
    end)
  end)
end

local function probe_story()
  if pc.storyprobe then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    pc.storyprobe = true
    LOG('storyprobe| getStoryTaskState(1000) = ' .. tostring(A:getStoryTaskState(1000)))
    LOG('storyprobe| getStoryTaskState(1003) = ' .. tostring(A:getStoryTaskState(1003)))
    LOG('storyprobe| isStoryTaskFinished(1000) = ' .. tostring(A:isStoryTaskFinished(1000)))
    local ok, st = pcall(function() return A:getSystemTaskState(1) end)
    LOG('storyprobe| getSystemTaskState(1) = ' .. tostring(ok) .. ' -> ' .. tostring(st))
    LOG('storyprobe| street_id = ' .. tostring(A:getStreetID()))
    -- 统计当前街区里 preTask 命中的 NPC
    local total, shown = 0, 0
    pcall(function()
      local actorData = DataTable('Common', 'StreetActorInfoData')
      for id, d in pairs(actorData) do
        if d.preTask then
          total = total + 1
          for tid, tstate in pairs(d.preTask) do
            if type(tid) == 'number' and A:getTaskStateByTaskID(tid) == tstate then
              shown = shown + 1
              if shown <= 5 then
                LOG('storyprobe| NPC ' .. tostring(id) .. ' 命中 task ' .. tostring(tid) .. ' state ' .. tostring(tstate))
              end
              break
            end
          end
        end
      end
    end)
    LOG('storyprobe| 带 preTask 的 NPC ' .. total .. ' 个，条件命中 ' .. shown .. ' 个')
  end)
end
local function probe_charbag()
  if pc.charbag then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    local A = GlobalData.Avatar
    if A == nil then return end
    pc.charbag = true
    local ids = {}
    pcall(function()
      for k, v in pairs(A.characterBag) do
        ids[#ids + 1] = tostring(k)
      end
    end)
    table.sort(ids)
    LOG('charbag| 拥有角色: ' .. table.concat(ids, ','))
    local ok, lv = pcall(function() return A:getCharacterLevel(1020) end)
    LOG('charbag| getCharacterLevel(1020) ok=' .. tostring(ok) .. ' -> ' .. tostring(lv))
    local ok2, sk = pcall(function() return A:getUsingSkinId(1020) end)
    LOG('charbag| getUsingSkinId(1020) ok=' .. tostring(ok2) .. ' -> ' .. tostring(sk))
  end)
end
local function probe_openmap()
  if pc.mapopened then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    pc.mapopened = true
    LOG('mapprobe| 打开 MapV2Panel')
    ui.OpenPanel(PanelNames.MapV2Panel, function(panel)
      local ok, err = pcall(function()
        panel:initMap(1, GlobalData.Avatar:getStreetID(),
                      SystemConst.MapPanelOpenType.fromSmallMap)
      end)
      LOG('mapprobe| initMap ok=' .. tostring(ok) .. ' err=' .. tostring(err))
    end)
  end)
end
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
    -- 体力（时之砂）
    local sta = tostring(A.tower_reward_keys)
    if pc.staLast ~= sta then
      LOG('stamina| #' .. pc.sigWatch .. ' keys=' .. sta
        .. ' next=' .. tostring(A.next_key_recover_timestamp)
        .. ' total=' .. tostring(A.total_key_recover_timestamp))
      pc.staLast = sta
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
OO_PROBE_MAP = false    -- 地图面板探针（会自动弹地图）
OO_PROBE_STREET = false -- 街区切换探针（会主动发切街区 RPC）
OO_PROBE_STORY_ADV = false -- 剧情推进探针（会模拟副本结算）
OO_PROBE_STORY_NPC = false -- 剧情 NPC 探针（结算前后数街上 actor）
OO_PROBE_STARTGAME = false -- 副本入口探针（发 StartGame 并打回调）
OO_PROBE_ENTERHUB = false  -- 直接调 enterOnlineHub 探针
OO_PROBE_HUBSTEPS = false  -- hub:init/getCopy 分步探针
OO_PROBE_LOADSTATE = false -- LoadingStage 就绪标志探针
OO_PROBE_LOADSTEP = false  -- 加载步骤探针
OO_PROBE_PRELOAD = false   -- PreLoadPanelByName 打桩探针
OO_PANEL_TIMEOUT = false   -- 诊断：面板预加载超时放行（资源已修复，不再需要）
OO_PROBE_NEXTLEVEL = false -- 自动触发进入下一关

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
    for _, nm in ipairs({
        -- 对照组：已知存在 / 已知缺失
        'sc_zhpmap_01', 'sc_mainstage',
        -- 序章副本关卡（LevelData[50001].levelData[1]）
        'sc_dgarea_prologue_zhp_lv_01',
        'scenes/Level/prologue/sc_dgarea_prologue_zhp_lv_01',
        'scenes/Level/sc_dgarea_prologue_zhp_lv_01',
        'scenes/Level/zhp/sc_dgarea_prologue_zhp_lv_01',
        -- 主线副本（MainlineData 里能查到的）
        'sc_dgarea_feiyi_lv_01_plot',
        'sc_dgarea_jiang_lv_03',
        'sc_dgarea_qianxianghui_lv_01',
        -- 战斗面板 prefab（panelmanager.lua:686 的路径格式）
        'ui/panels/Battle_Main_Panel/Battle_Main_Panel',
        'ui/panels/BattleHpPanel/BattleHpPanel',
        'ui/panels/MessagePanel/MessagePanel',
        'ui/panels/BlackPanel/BlackPanel',
      }) do
      local ok, v = pcall(function() return inst:IsAssetExist(nm) end)
      LOG('csapi| exist2 ' .. nm .. ' = ' .. tostring(ok and v))
    end
  end)

  -- 把资源表整体打出来（游戏自己的 GM 功能，只读）
  local okDump, errDump = pcall(function() inst:DumpResourceInfo() end)
  LOG('csapi| DumpResourceInfo ok=' .. tostring(okDump) .. ' err=' .. tostring(errDump))
end
