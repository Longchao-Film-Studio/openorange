-- oo/05b_probe.lua —— 05a 的续（同一批探针，按文件名顺序拼接）
--
-- ★ 2026-10-24：本文件是诊断探针（面板超时观测、字段 dump 等），非生产逻辑。
--   默认跳过全部内容；设 OO_DIAGNOSTICS=1（启动器环境变量）才启用。
--   生产逻辑 install_panel_timeout 已迁至 06b_hooks.lua。
if rawget(_G, "OO_DIAGNOSTICS") ~= "1" then
  return
end

-- install_panel_timeout 已迁至 06b_hooks.lua（生产逻辑不应混在探针里，2026-10-24）

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
-- ★ P1 阶段 I：给 AvatarStoryTaskComp 的 onStoryTaskItemChanged 打钩子。
--   这是本次开发的核心未证事项：服务端发的 OP_CHANGE 到底能不能让客户端
--   这个回调真的触发。以前只能证到「客户端登录进街区无报错」（见 docs A6/B6），
--   「帧被回调接受」一直是未证状态，所以必须真的挂一次钩子数一数。
--   依据：generated/config.lua:9933-9936  changed["story_task_bag.*"] ->
--         onStoryTaskItemChanged（avatarstorytaskcomp.lua:100-109）
local function install_story_cb_hook()
  if pc.storycb then return end
  pcall(function()
    local comp = GlobalData.Avatar
    if comp == nil then
      -- ★ 不要静默 return：以前这里不打日志，导致「钩子没装上」时
      --   日志里一条线索都没有，只能靠猜（2026-10-22 实机就踩了）。
      --   现在留痕，且不设 pc.storycb，下一轮会重试。
      if (pc.storycb_nolog or 0) < 3 then
        pc.storycb_nolog = (pc.storycb_nolog or 0) + 1
        LOG('storycb| 暂不装钩子：GlobalData.Avatar 还是 nil（第 '
          .. pc.storycb_nolog .. ' 次，稍后重试）')
      end
      return
    end
    local orig = comp.onStoryTaskItemChanged
    if type(orig) ~= 'function' then
      if (pc.storycb_nolog or 0) < 3 then
        pc.storycb_nolog = (pc.storycb_nolog or 0) + 1
        LOG('storycb| 暂不装钩子：Avatar 上没有 onStoryTaskItemChanged（类型='
          .. type(orig) .. '，第 ' .. pc.storycb_nolog .. ' 次，稍后重试）')
      end
      return
    end
    pc.storycb = true
    pc.storycb_hits = 0
    pc.storycb_last = ''
    comp.onStoryTaskItemChanged = function(self, old, new)
      pc.storycb_hits = pc.storycb_hits + 1
      local function desc(x)
        if type(x) ~= 'table' then return tostring(x) end
        -- ★ 不能用 rawget：客户端的属性对象是带元表的自定义类型，
        --   字段走 __index（实机踩过：rawget 全返回 nil，日志只有 task=nil）。
        local tid, stt
        pcall(function() tid = x.id end)
        pcall(function() stt = x.state end)
        if tid == nil and stt == nil then
          -- 兜底：扫一遍已知字段名
          pcall(function()
            for _, k in ipairs({"id","state","taskId","task_id"}) do
              if x[k] ~= nil then
                if k == "id" or k == "taskId" or k == "task_id" then tid = x[k] end
                if k == "state" then stt = x[k] end
              end
            end
          end)
        end
        return 'task=' .. tostring(tid) .. ' state=' .. tostring(stt)
      end
      pc.storycb_last = desc(old) .. ' -> ' .. desc(new)
      LOG('storycb| 第 ' .. pc.storycb_hits .. ' 次触发 ' .. pc.storycb_last)
      return orig(self, old, new)
    end
    LOG('storycb| onStoryTaskItemChanged 钩子已安装')
  end)
end

-- 汇报钩子累计触发次数（进街区后过几秒调一次）
local function report_story_cb()
  if pc.storycbrep then return end
  pcall(function()
    local stage = game.stageMgr:getCurStage()
    if stage == nil or stage.name ~= GlobalConst.EGameStage.Street then return end
    -- ★ 只有钩子真的装上了才报告并锁存（2026-10-22 实机踩的坑：
    --   以前在 frame 20 就报「未安装」并置 pc.storycbrep = true，
    --   而安装是在 frame 30 —— 报告先于安装，且锁死后再也不报，
    --   于是日志里只剩一条误导性的「未安装」。）
    if not pc.storycb then
      return   -- 不锁存，等钩子装好下一轮再报
    end
    pc.storycbrep = true
    LOG('storycb| 报告：累计触发 ' .. tostring(pc.storycb_hits) .. ' 次，最后一次 ' .. tostring(pc.storycb_last))
    -- 顺带报另外三个回调面，确认它们也通了
    local A = GlobalData.Avatar
    if A then
      pcall(function()
        local n = 0
        for k in pairs(A.story_task_bag or {}) do
          if k ~= '__id__' and k ~= '__tp__' then n = n + 1 end
        end
        LOG('storycb| story_task_bag 条目数 = ' .. n)
      end)
      pcall(function()
        local n = 0
        for k in pairs(A.done_story_tasks or {}) do
          if k ~= '__id__' and k ~= '__tp__' then n = n + 1 end
        end
        LOG('storycb| done_story_tasks 条目数 = ' .. n)
      end)
      pcall(function()
        LOG('storycb| story_finished[1] = ' .. tostring((A.story_finished or {})[1]))
      end)
      pcall(function()
        local n = 0
        for k in pairs(A.story_max_collect_num or {}) do
          if k ~= '__id__' and k ~= '__tp__' then n = n + 1 end
        end
        LOG('storycb| story_max_collect_num 条目数 = ' .. n)
      end)
      pcall(function()
        local n = 0
        for k in pairs(A.story_reward_list or {}) do
          if k ~= '__id__' and k ~= '__tp__' then n = n + 1 end
        end
        LOG('storycb| story_reward_list 条目数 = ' .. n)
      end)
      pcall(function()
        local n = 0
        for k, v in pairs(A.task_bag or {}) do
          if k ~= '__id__' and k ~= '__tp__' then n = n + 1 end
        end
        LOG('storycb| task_bag 槽位数 = ' .. n)
      end)
    end
  end)
end

-- 探针开关：需要跑 1-3 端到端 RPC 验证时置 true
OO_PROBE_RPC = false    -- Set* 系列端到端探针（会改数据，默认关）
OO_PROBE_WRITE = false  -- UseItem / UpgradeCharacter 写操作探针（会改数据）
OO_PROBE_STORY_CB = true   -- P1-I：onStoryTaskItemChanged 钩子（默认开，只读不改数据）
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
