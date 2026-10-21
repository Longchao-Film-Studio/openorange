-- oo/05c_probe.lua —— 05b 的续（同一批探针，按文件名顺序拼接）
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
