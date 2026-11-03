#!/usr/bin/env python3
"""客户端契约：客户端「读什么」<-> 服务端「给什么」的唯一真相源。

## 这个模块为什么存在

客户端大量代码直接索引 `GlobalData.Avatar.<属性>`，属性是 nil 就当场崩，
表现是「面板打开即自我关闭」= 用户看到「点了没反应」。这件事**已经发生过两次**：

  1. `dungeonView` 缺失  -> 按 M 打开地图崩 -> 「UI 闪一下又回来」
  2. `title_daily_task_cache` 缺失 -> 点头衔入口崩 -> 「点了没反应」（2026-10-28）

两次都要靠人翻 `Player.log` 找 `attempt to index a nil value` 才定位，
然后手工判断类型、手工加进生成器。这是**补丁驱动的典型形态**：
同一个坑换个属性再来一次，每次都要人肉排查。

本模块把这类知识收敛到一处，做到：

    发现（扫客户端 Lua 的读点）-> 解析（类型从哪来）-> 报告（缺什么、会崩哪个面板）

生成器（tools/build_avatar_init.py）和守卫测试（tests/test_avatar_prop_coverage.py）
都从这里取数，不再各存一份。

## 四类属性，四种来源（判定顺序固定）

| 类别 | 来源 | 例子 |
|---|---|---|
| 已下发 | 生成器**已经**写进 avatar_init.json | `level` |
| SERVER_PROPS | 显式声明**自定义类型**（人工判断过，带依据） | `task_bag -> TaskTypeBag` |
| SIMPLE_PROPS | 显式声明**简单类型初值**（不是对象，只是一个值） | `street_id -> 101` |
| EXEMPT | 客户端自己造的 / 引擎对象 / 不必下发（带理由） | `destroyed` |
| 自动推导 | 名字 snake_case -> PascalCase，命中已声明自定义类型（**只做候选**） | `title_daily_task_cache -> TitleDailyTaskCache` |
| 未解释 | 以上都不满足 -> **守卫测试必须红** | —— |

SERVER_PROPS 和 SIMPLE_PROPS 的区别不是「重不重要」，而是客户端怎么用它：
前者要预置一个**带 `__tp__` 的对象**（客户端会 `pairs()` 或按字段索引），
后者只是一个**标量初值**（客户端直接比较 / 加减）。混淆的后果不对称：
给标量塞个对象 -> `assert(customType ~= nil)` 崩；
给对象塞个标量 -> 客户端 `pairs(nil)` 或索引 nil 崩。所以必须分开管。

## 为什么不「全自动推导」了事（实测反例）

`task_bag` 按名字推导会得到 `TaskBag`（单层字典），但客户端是
`task_bag[taskType][taskID]` 两层的，正确类型是 `TaskTypeBag`。
用 `TaskBag` 客户端**不报错**，只是永远取不到数据 —— 比崩溃更难查。

所以自动推导只做**候选**，凡是「推导会错」的必须进 SERVER_PROPS 覆盖并写理由。
这条规则由 tests/test_client_contract.py 的负例断言守住。

## 命令行

    python tools/client_contract.py            # 打印完整契约报告
    python tools/client_contract.py --gaps     # 只打印未解释的缺口（退出码 1 表示有缺口）
"""
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 客户端 decompile 出来的 Lua 根目录（与 tests/lua_harness.py 同一个环境变量）
def _find_lua_root():
    """定位反编译出来的客户端 Lua 根目录（含 generated/config.lua）。

    ★ 2026-11-03：项目随磁盘换过盘符（F: -> D:），硬编码路径会让所有依赖本契约的
      测试全部跳过或报错。改成：
        OO_LUA_ROOT 环境变量 > 相对本仓库的 ../archive/... > 常见历史位置
    """
    env = os.environ.get("OO_LUA_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.normpath(os.path.join(here, "..", "archive", "game_data",
                                         "LuaAll_Decompiled2"))
    if os.path.isdir(cand):
        return cand
    for drive in ("D:", "F:", "E:", "C:"):
        alt = os.path.join(drive + os.sep, "haowancheng", "archive", "game_data",
                           "LuaAll_Decompiled2")
        if os.path.isdir(alt):
            return alt
    return cand


LUA_ROOT = _find_lua_root()

#: 属性式访问：GlobalData.Avatar.<name> 后面不能紧跟 "(" 或 ":"（那是方法调用）
PROP_RE = re.compile(r"GlobalData[.]Avatar[.]([A-Za-z_][A-Za-z0-9_]*)(?![\w:(])")

#: 第一类：客户端读不到就崩、但**名字推导不出正确类型**的属性 -> 类型。
#:
#: 这是契约的唯一权威表：每条都有人工判断过的依据（注释就写在条目上）。
#: 自动推导能覆盖的**不**写在这里，由 derive_type() 现场算候选。
SERVER_PROPS = {
    # --- AvatarBagComp:refreshLocalBagData -> pairs(self.item_bag) ---
    "item_bag": "ItemBag",
    # --- AvatarTaskComp:initDoneAchievement / getAllDoingDailyTaskItem -> task_bag ---
    # ★ task_bag 是**两层**的：task_bag[taskType][taskID]。
    #   依据 generated/config.lua:10123 的回调路径 ["task_bag.*.*"]，
    #   以及 avatartaskcomp.lua:257  getTaskState: self.task_bag[taskType][taskID]。
    #   所以类型是 TaskTypeBag（Dict<int, TaskBag>），不是 TaskBag。
    #   以前写成单层 TaskBag，getSystemTaskState 走 task_bag[type][id] 时
    #   索引的是 TaskItem 对象，永远取不到，只能靠 done_tasks 兜底。
    "task_bag": "TaskTypeBag",
    # --- AvatarActivityComp -> pairs(cur_activity_info) ---
    "cur_activity_info": "ActivityProp",
    # --- AvatarAchieveComp:initDoneAchievement -> pairs(achieve_type_bag) ---
    "achieve_type_bag": "AchieveTypeBag",
    # --- AvatarCharacterComp -> pairs(characterBag) / characterBag[charId].xxx ---
    # ★ 2026-10-19 修正：原来写成 CharacterList 是错的。
    #   CharacterList 继承 CustomList（列表），而 avatarcharactercomp 是这么用的：
    #     :79   self.characterBag[charID].biography_read
    #     :533  self.characterBag[charId].breakAward
    #     :811  return self.characterBag[charId].useSkinId
    #     :822  self.characterBag[charId].skins[charAppearanceId]
    #   这些都是 CharacterInfo 的字段；列表没法按 charId 索引。
    #   config.lua 里 CharacterPersistentDict 才是 __ValueType__=CharacterInfo、
    #   __IntTypeKey__=true 的字典，而且
    #   gamecommon/customtypes/character/characterpersistentdict.lua 上就写着
    #   HasChar(no) / GetAllSkins / CheckAddSkin，正是 characterBag 的用法。
    "characterBag": "CharacterPersistentDict",
    # --- AvatarStreetComp -> pairs(street_info) / pairs(street_dispatch) ---
    "street_info": "StreetInfoDict",
    "street_dispatch": "StreetDispatchDict",
    # --- AvatarStorylineComp -> self.story_finished[id] or false ---
    "story_finished": "BooleanDictKeyInt",
    "story_max_collect_num": "IntDictKeyInt",
    "story_reward_list": "IntListDictKeyInt",
    # --- AvatarSystemGuideComp -> pairs(system_guide_records) ---
    "system_guide_records": "IntDictKeyInt",
    "battle_teach_state": "IntDictKeyInt",
    # --- PartyGameDataModel:15 -> self.partygame_p ---
    "partygame_p": "PartyGameProp",
    # --- 其余来自 AvatarProperties 的遍历型属性 ---
    "accu_charge_gacha_p": "AccuChargeGachaSetProp",
    "bp_info": "BpInfo",
    "card_prop": "CardDictProp",
    "championship_data": "ChampionshipData",
    "chat_p": "ChatProp",
    "contribution_player_cycle": "ContributionPlayerInfoRecord",
    "gacha_prop": "GachaProp",
    "multitower_task_prop": "ActiveTaskBag",
    "popup_gift_p": "PopupGiftProp",
    "returning_player_act_p": "ReturningPlayerActProp",
    "rogue_p": "RogueProp",
    "street_info_2d": "StreetInfoDict2D",
    "weekly_play_bp_p": "WeeklyPlayBPProp",
    "soul_task_bag": "SoulTaskBag",
    "story_task_bag": "StoryTaskBag",
    "done_tasks": "DoneTaskTypeBag",
    "done_story_tasks": "StoryTaskBag",
    # --- 导演头衔面板的每日任务缓存（H3 实机崩溃修复） ---
    # 2026-10-28 实机：点左上角等级/头衔入口 -> DirectorTitleV3Panel 打开后
    # 立刻报错并自我关闭，用户看到的就是「点了没反应」。
    #   ui/panel/directortitlev3panel/node_daily_task_item/node_daily_task_itemgroup.lua:86
    #     local firstDay = GlobalData.Avatar.title_daily_task_cache.is_first_day ~= 1
    #   <- refreshState (86) <- showTaskInfo (67) <- setData (38)
    #   <- DirectorTitleV3Panel.lua:198 _initDailyTaskList <- :167 initTaskInfo <- :35 initEx
    # 属性是 nil -> attempt to index a nil value。
    #
    # ★ 这个属性在 config.lua 里**完全不存在**（AvatarProperties 173 项里没有，
    #   PropertyCallbacks 里也没有），但类型 TitleDailyTaskCache 是声明过的
    #   （config.lua:7258，字段 is_first_day/fst_type_tasks/sed_type_tasks/fst_task/sed_task）。
    #   客户端只有这一个读点、没有任何地方创建它 —— 说明官方服务端是在运行时
    #   用 _changeRootProperty 动态建的（和 singleMatchInfo 同一类）。
    #   我们改从 initDict 建，等价，且不需要新增协议。
    #
    # 安全性依据（不是猜）：core/propertysync/puppetpropertycomponent.lua:47-65
    #   的 initProperty 遍历服务端下发的 __Properties__，只按 value.__tp__ 建对象，
    #   **不查 AvatarProperties 声明**。所以下发一个未声明属性不会触发任何断言。
    "title_daily_task_cache": "TitleDailyTaskCache",
    # --- 街区等级信息（同一类：客户端读了、我们没下发） ---
    # ui/panel/streetpanel/node_pop_achievement_reward/node_pop_achievement_rewardgroup.lua
    # 读 GlobalData.Avatar.street_level_info；类型 StreetLevelInfo 在 config.lua 有声明。
    "street_level_info": "StreetLevelInfo",
    # --- 位置持久化 ---
    # street_pos_info 只在 PropertyCallbacks 里声明（config.lua:10011），
    # AvatarProperties 没有它，所以生成器不会自动建。
    # 客户端 system/streetsystem/manager/streetsystemmgrbase.lua:561-566 读它恢复出生点：
    #   local posinfo = obj:getStreetPosInfo()
    #   if posinfo and posinfo.x then remotePosInfo = copy(posinfo) end
    # 然后 :654 用 remotePosInfo.x/y/z 定位、remotePosInfo.dir 定朝向。
    #
    # 客户端没有任何类型同时带 x/y/z/dir（全表搜过），所以用 Pos（x/y/z, double）。
    # dir 为 nil 时 setRotation(x, nil, z) 会被 xlua 转成 0 —— 位置对了，朝向取默认。
    # ★ 用 DoubleDict（Dict<string,double>）—— 不是 Pos。
    #   踩过的坑：用 Pos（{x,y,z} 结构体）会让客户端
    #     ClientAvatar function init must have a boolean return value
    #   traceback -> AvatarBagComp:refreshLocalBagData，必现。
    #   原因：street_pos_info 在 PropertyCallbacks 里声明的是 customDict，
    #   而 Pos 不是 dict 类型，初始化时的回调注册对不上。
    #
    #   DoubleDict 才是对的：core/propertysync/customdict.lua:21-30 的 __index
    #   直接读 _properties[k]，所以客户端 streetsystemmgrbase.lua:562 的
    #     if posinfo and posinfo.x then ...
    #     for key, value in pairs(posinfo) do remotePosInfo[key] = value end
    #   两处都能正常工作，而且 dir 也能带上（Pos 没有 dir 字段）。
    "street_pos_info": "DoubleDict",
    # ★ 2026-10-20 试过但**回滚**了：给 street_pos_info 用 Pos 类型会破坏客户端。
    #   加了之后实机必现：
    #     class ClientAvatar function init must have a boolean return value
    #     traceback -> AvatarBagComp:refreshLocalBagData
    #   去掉就恢复正常。推测是它的 PropertyCallbacks 声明是 customDict，
    #   而 Pos 不是 dict 类型，初始化时的回调注册对不上。
    #   位置持久化暂时不做（_oo_street_pos 仍在服务端存着，等找到对的类型再说）。
    # --- 动态根属性：只出现在 PropertyCallbacks、AvatarProperties 里没有声明 ---
    # 证据：generated/config.lua:6370 的 Custom.SingleMatchInfo 定义了
    #   state(int) / match_id(string) / match_info(MatchInfo)；
    #   entities/avatarcomponent/avatarteamcomp.lua:648 不判空直接
    #   self.singleMatchInfo.state —— 空指针就会把 HallStage 进场打断。
    # 真实服务端是用 _changeRootProperty 运行时建这个属性（带 __tp__），
    # 我们改从 initDict 建，等价。
    "singleMatchInfo": "SingleMatchInfo",
    # --- 同样「只在 PropertyCallbacks 里声明」的属性 ---
    # 扫描方法：从 generated/config.lua 的 PropertyCallbacks 块里抽出所有属性名，
    # 和 initDict 对差 —— 得到 15 个缺失项。其中只有这两个在客户端有对应
    # 自定义类型声明（config.lua:3012-3048 DungeonViewDict / EndlessCharacterList），
    # 其余 10 个客户端连类型都没声明，塞进去会触发
    #   puppetpropertycomponent.lua:53  assert(customType ~= nil)
    # 直接崩，所以不能加。
    #
    # dungeonView 缺失的后果（实测）：按 M 打开地图时
    #   MapV2Panel:initMap -> initAreaListRequire -> processingAreaMapTabList
    #   -> AvatarDungeonComp:getDungeonAllCollectCoins(10101)
    #   -> self.dungeonView[areaId]  (attempt to index a nil value)
    #   面板被 closePanelEx 关掉，表现为「UI 闪一下又回来」。
    "dungeonView": "DungeonViewDict",
    "endless_character_list": "EndlessCharacterList",
    # mainline_level：avatarstreetcomp.lua:266 的
    #   local level = GlobalData.Avatar.mainline_level[area_id]
    # 说明是 Dict<area_id, level(int)> —— 就是 IntDictKeyInt。
    "mainline_level": "IntDictKeyInt",
    # --- AvatarFormationComp 的编队数据 ---
    # 依据 entities/avatarcomponent/avatarformationcomp.lua:32-37：
    #   function AvatarFormationComp:getFormationInfo(index)
    #     return self.character_list[index] or { value = {}, name = "" }
    #   end
    # 兜底值正好是 Custom.Character 的两个字段（config.lua:1907-1923：
    #   name = string, value = IntList），而 CharacterList 的
    #   __ValueType__ = "Character"（config.lua:2074-2078）。
    # config.lua:8388 的 PropertyCallbacks 是 ["character_list.*"]，
    # 说明它是按下标取的自定义容器。
    # 注意：之前几轮的对差脚本漏了它，因为 key 里带 * 号，
    # 而那个脚本的正则字符类没包含 *。
    "character_list": "CharacterList",
}

#: 第二类：客户端读了但**不需要服务端下发**的名字 -> 理由。
#:
#: 纪律（见 tests/test_client_contract.py）：
#:   每条必须是「客户端自己造的 / 引擎对象 / 死代码 / 原始类型 nil 安全」之一，
#:   不许写「暂时没有也没事」。且每条必须仍然被客户端读到（否则算腐烂，测试会红）。
EXEMPT = {
    # --- 引擎 / 框架对象，不是玩家属性 ---
    "destroyed": "Unity 对象自带的 destroyed 标志（net/msnetworkmanager.lua），不是属性",
    "reconnectInfo": "网络管理器自己的重连信息（net/networkmanager.lua），客户端本地状态",
    "recordTimeDelay": "RTM 上报节流时间（manager/rtmclientmanager.lua），客户端本地状态",
    "__Properties__": "属性容器本身（modules/gmcommand/gmavatarproperties.lua 遍历用）",
    # --- GM / 调试专用，正式服不执行 ---
    "gmUnlockDyeColors": "GM 命令强制解锁染色（modules/gmcommand/gmcommand.lua），仅 DEBUG_MODE",
    "canEnterDyneStage": "GM 命令直进关卡（modules/gmcommand/gmcommand.lua），仅 DEBUG_MODE",
    # --- 客户端本地回调 / 临时态 ---
    "curTrialSkinExitCb": "试用皮肤退出回调函数（generalsettlementpanel），客户端自己赋值",
    "isInRoleHome": "角色主页状态判定（stage/fastjumpstage.lua），由客户端 UI 流程赋值",
    # --- 模板/笔误，不是真实属性 ---
    "xx": "item_filter 里的模板占位（node_pop_item_filtergroup.lua），非真实属性",
    # --- 原始类型：nil 不会崩（只参与比较/算术的地方有兜底）---
    # 这些是服务端运行时才会写的计数字段，新号读到 nil 是安全的：
    # 客户端只在「已经发生过对应行为」的分支里读它们。
    "enterSpaceCount": "进副本计数，只在已进过副本的分支读；nil 安全（streetsystemmgrbase）",
    "done_achievement": "成就完成集合，achievementdatamodel 有 or {} 兜底",
    "bp_level": "战令等级，node_reward 用 or 0 兜底",
    "bp_exp": "战令经验，taskpanel 用 or 0 兜底",
    "endless_score_award_record": "无限模式积分领奖记录，levelendlesspanel 有兜底",
    "endless_rank_id": "无限模式排名 id，只在有排名时读",
    "fishingMinerActivityId": "钓鱼活动 id，活动未开时该分支不执行",
    "street_param": "街景参数，主线结算面板有兜底",
    "puzzle_passed_time": "解谜通关时间，只在通关后读",
    "friendBlackList": "好友黑名单，好友面板管理弹窗有兜底",
    "recordChallengeBossEnterActivityId": "高难 BOSS 活动 id，结算面板有兜底",
    "activityNewRedDict": "活动红点字典，node_activity_entrance 有兜底",
    "activityNewRedDictWithActID": "活动红点字典（带 actId），同上",
}

#: 推导会给出**错误**答案的名字 -> (正确类型, 为什么推导是错的)。
#:
#: 这是自动推导唯一不能替代人的地方：名字看起来能推，但语义不对。
#: 每条都必须有实机/代码依据，且由 tests/test_client_contract.py 的负例守住。
DERIVE_TRAPS = {
    "task_bag": (
        "TaskTypeBag",
        (
            "名字推导得到 TaskBag（单层），但客户端是 task_bag[taskType][taskID] 两层。"
            "avatartaskcomp.lua:257 getTaskState 就是这个用法；"
            "用单层客户端不报错、永远取不到数据。"
        ),
    ),
}


#: 第三类：不必预置自定义对象、但**必须给个初始值**的简单类型根属性 -> (默认值, 依据)。
#:
#: 这些只出现在 PropertyCallbacks 里（AvatarProperties 没有声明），客户端访问时
#: 基本不判空，nil 会让面板建了又关。简单值不会触发 __tp__ 断言，塞进去是安全的。
#: 依据来源：work/oo_analysis/missing_props.py 把各 Avatar*Comp 的 PropertyCallbacks
#: 段和 AvatarProperties 对差，再按回调里声明的类型（number/string/boolean）分类。
#:
#: 和 SERVER_PROPS 的区别：这里是**值**不是类型，生成器直接写进 initDict。
SIMPLE_PROPS = {
    "msAuth": ("", "PropertyCallbacks 里声明、客户端没给自定义类型；空串是唯一安全的初值"),
    "test_soul_count": (0, "同上；测试用计数，客户端直接加减"),
    "avatarNowState": (0, "同上；头像/大厅状态，0 = 初始"),
    "avatarTeamMatchState": (0, "同上；队伍匹配状态，0 = 空闲"),
    "battle_prop_choosen_index": (0, "同上；战斗属性选项下标，0 = 未选"),
    # ★ 0 = 新号还没有主角。
    #   以前这里写死 9001，等于新号一出生就有男导演 —— 选女号时再补 9002，
    #   结果两个导演并存（实机 bug）。现在新号没有角色，取名时按性别创建唯一主角
    #   （game_server/protagonist.py 是唯一入口）。
    #   system/streetsystem/manager/streetplayerrolemgr.lua:8-18 读它是为了**进街区后**
    #   加载玩家模型，而进街区的前置条件（streetstage.lua:167-182）就要求
    #   已取名 + 有主角，所以 0 不会导致「场景有了但没角色」。
    "character_choosen_char": (0, "当前跟随街区的角色 id；0 = 新号未取名，取名时由 protagonist.py 写入"),
    "choosen_index": (1, "编队下标，1 = 第一队（与生成器的 STARTER_FORMATION 对应）"),
    "daily_active_value": (0, "日常活跃度"),
    "endless_choosen_index": (0, "无限模式编队下标"),
    "four_racing_character": (0, "四人竞速角色，0 = 未选"),
    "has_seen_unlock_action": (False, "是否看过解锁演出；False = 没看过"),
    "is_rank_mode_available": (False, "排位模式是否开放；新号不开"),
    "mvp_count": (0, "MVP 计数"),
    "normal_match_count": (0, "普通对局场次"),
    "normal_mode_win_count": (0, "普通模式胜场"),
    "normal_mvp_count": (0, "普通模式 MVP 次数"),
    "punishment_end_time": (0, "惩罚结束时间戳，0 = 无惩罚"),
    "puzzle_character": (0, "解谜玩法角色，0 = 未选"),
    "rank_match_count": (0, "排位对局场次"),
    "rank_mode_win_count": (0, "排位胜场"),
    "rank_mvp_count": (0, "排位 MVP 次数"),
    "rank_score": (0, "排位分数"),
    # ★ 必须 101，不能 0。依据 manager/loginmanager.lua:118-135：
    #     local street_id = player:getStreetID()
    #     if ... then elseif street_id ~= nil and street_id ~= 0 then
    #         JumpToStreetStage("StreetData", street_id)   <- 进主城街区
    #     else if story_finished[1] then JumpToHallStage() end
    #   走 else 分支的 JumpToHallStage() 需要 sc_mainstage 场景，
    #   而这一版客户端资源里**没有**该场景（ROADMAP 第 9/15/19 节四条独立证据），
    #   于是黑屏 + 断线。CommonConstData.DefaultStreet = 101（中浦区），
    #   TableData.CommonData.StreetData[101].scene = sc_zhpmap_01_streaming 存在。
    "street_id": (101, "登录落地的街区 id；101 = 中浦区。0 会走 JumpToHallStage 而该场景缺失 -> 黑屏"),
    "street_trace_taskid": (0, "街区追踪任务 id，0 = 无"),
    "temp_floor": (0, "临时层数（爬塔）"),
    "winNum": (0, "通用胜场数"),
}


@dataclass(frozen=True)
class Resolution:
    """一个属性读点的解析结果。

    kind 的含义（判定顺序固定）：
      provided  服务端**已经**下发（在 avatar_init.json 里）—— 无需处理
      exempt    客户端自己造的 / 引擎对象 / 不必下发（带理由）
      server    SERVER_PROPS 里声明了，但生成器没产出 —— 生成器 bug
      derivable 名字能推导出已声明类型，但还没下发 —— **可自动修**
      unknown   以上都不是 —— 需要人判断

    simple    SIMPLE_PROPS 里给了初始值，但生成器没写进 initDict —— 生成器 bug
    """

    name: str
    kind: str
    type: str | None
    reason: str
    files: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """这个读点有解释吗（provided / exempt 算有）。"""
        return self.kind in ("provided", "exempt")


def _declared_custom_types() -> dict:
    """config.lua 里声明过的自定义类型（懒加载，避免 import 时读盘）。"""
    from game_server.property import spec  # noqa: PLC0415

    return spec._load().get("custom_types") or {}


def provided_props() -> set[str]:
    """服务端**当前**下发的属性名集合（来自生成的 avatar_init.json）。"""
    from game_server import ms  # noqa: PLC0415

    return set(ms.AVATAR_INIT_DICT.get("__Properties__") or {})


def derive_type(name: str, declared: dict | None = None) -> str | None:
    """从属性名推导已声明的自定义类型。推不出返回 None。

    规则（两种命名都覆盖，按顺序试）：
      snake_case  title_daily_task_cache -> TitleDailyTaskCache
      camelCase   singleMatchInfo        -> SingleMatchInfo

    ★ 推导只产出**候选**，不自动采用。语义可能不对的见 DERIVE_TRAPS。
    """
    if declared is None:
        declared = _declared_custom_types()
    pascal = "".join(p[:1].upper() + p[1:] for p in name.split("_"))
    camel = name[:1].upper() + name[1:]
    for cand in (pascal, camel):
        if cand in declared:
            return cand
    return None


def discover(lua_root: str | None = None) -> dict[str, tuple[str, ...]]:
    """扫客户端 Lua，返回 {属性名: (读点文件, ...)}。只收属性式访问。"""
    root = lua_root or LUA_ROOT
    where: dict[str, list[str]] = defaultdict(list)
    if not os.path.isdir(root):
        return {}
    for dirpath, _dirs, names in os.walk(root):
        for fn in names:
            if not fn.endswith(".lua"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                txt = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            for m in PROP_RE.finditer(txt):
                where[m.group(1)].append(rel)
    return {k: tuple(sorted(set(v))) for k, v in where.items()}


def resolve(name: str, files: tuple[str, ...] = (),
            declared: dict | None = None, provided: set[str] | None = None) -> Resolution:
    """判定一个属性读点的来源。

    ★ 顺序是判据的一部分，不能改：
      1) provided 最先 —— 已经在 avatar_init 里就不用管它是怎么来的；
      2) exempt 次之 —— 豁免表示「不需要下发」，不该被推导成「应该下发」；
      3) SERVER_PROPS —— 人工判断过（含推导会错的，见 DERIVE_TRAPS）；
      4) 推导 —— 只给候选，不自动采用；
      5) 剩下的才是 unknown。
    """
    if provided is None:
        provided = provided_props()
    if name in provided:
        return Resolution(name, "provided", None, "已在 avatar_init.json 里下发", files)
    if name in EXEMPT:
        return Resolution(name, "exempt", None, EXEMPT[name], files)
    if name in SERVER_PROPS:
        return Resolution(name, "server", SERVER_PROPS[name],
                          "SERVER_PROPS 声明了但生成器没产出（生成器 bug）", files)
    if name in SIMPLE_PROPS:
        return Resolution(name, "simple", None,
                          f"SIMPLE_PROPS 声明了初值 {SIMPLE_PROPS[name][0]!r}，"
                          "但生成器没写进 initDict（生成器 bug）", files)
    typ = derive_type(name, declared)
    if typ:
        return Resolution(name, "derivable", typ,
                          f"名字推导命中 {typ}；确认语义后加进 SERVER_PROPS", files)
    return Resolution(name, "unknown", None,
                      "没有 provided / EXEMPT / SERVER_PROPS / 推导 任一来源", files)


def classify(lua_root: str | None = None) -> dict[str, Resolution]:
    """扫一遍并逐个解析，返回 {属性名: Resolution}。"""
    found = discover(lua_root)
    declared = _declared_custom_types()
    provided = provided_props()
    return {name: resolve(name, files, declared, provided)
            for name, files in found.items()}


def required_types() -> dict[str, str]:
    """生成器要预置的 {属性名: 类型}。

    ★ 只返回 SERVER_PROPS —— **不含**自动推导的结果。
      原因：推导会错（见 DERIVE_TRAPS 的 task_bag），自动加进 avatar_init 会造出
      「类型存在但语义错」的属性，客户端不报错、只是静默取不到数据，比崩溃更难查。
      推导的结果由 classify() 作为**建议**报出来，由人确认后写进 SERVER_PROPS。
    """
    return dict(SERVER_PROPS)


def required_simple_props() -> dict[str, object]:
    """生成器要预置初值的简单类型属性 -> 默认值（SIMPLE_PROPS 的 {名字: 值} 视图）。"""
    return {k: v[0] for k, v in SIMPLE_PROPS.items()}


def gaps(lua_root: str | None = None) -> list[Resolution]:
    """没有解释的属性读点（守卫测试要求这个列表为空）。"""
    return sorted((r for r in classify(lua_root).values() if not r.ok),
                  key=lambda r: (r.kind, -len(r.files), r.name))


def stale_exempt(lua_root: str | None = None) -> list[str]:
    """EXEMPT 里已经不被客户端读到的条目（腐烂检查）。"""
    found = discover(lua_root)
    return sorted(k for k in EXEMPT if k not in found)


def _main() -> int:
    only_gaps = "--gaps" in sys.argv[1:]
    res = classify()
    kinds: dict[str, int] = defaultdict(int)
    for r in res.values():
        kinds[r.kind] += 1

    if not only_gaps:
        print(f"客户端属性读点：{len(res)} 个不同名字")
        for k, label in (("provided", "已下发"), ("exempt", "EXEMPT 豁免"),
                         ("server", "SERVER_PROPS 声明了但没产出"),
                         ("simple", "SIMPLE_PROPS 声明了初值但没产出"),
                         ("derivable", "可推导（建议加进 SERVER_PROPS）"),
                         ("unknown", "需要人判断")):
            print(f"  {label:<32} {kinds[k]}")
        print()

    derivable = sorted((r for r in res.values() if r.kind == "derivable"),
                       key=lambda r: r.name)
    if derivable and not only_gaps:
        print("推导出候选类型的（确认语义后加进 SERVER_PROPS）：")
        for r in derivable:
            print(f"  {r.name:<34} -> {r.type:<28} {r.files[0]}")
        print()

    g = gaps()
    if g:
        print(f"★ 未解释的读点（守卫测试会红）：{len(g)} 个")
        for r in g:
            print(f"  [{r.kind:<10}] {r.name:<32} {len(r.files)} 处"
                  f"  例如 {r.files[0]}")
        print()
        print("怎么修：")
        print("  derivable -> 确认类型对不对，对就加进 SERVER_PROPS")
        print("  unknown   -> 查客户端读点，判断该下发（SERVER_PROPS）还是豁免（EXEMPT）")
        print("  server    -> 生成器 bug：SERVER_PROPS 有但 avatar_init 没产出")
        print("  simple    -> 生成器 bug：SIMPLE_PROPS 有但 avatar_init 没产出")
        return 1

    if not only_gaps:
        print("没有未解释的读点。")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
