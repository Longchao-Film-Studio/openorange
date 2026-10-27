#!/usr/bin/env python3
"""RPC 索引表 —— 名字 -> genRpcIndex，以及应答常量。

从 entity_rpc.py 拆出来的纯数据模块（P0-E1）。这里不放任何业务逻辑：
它的唯一职责是「名字 <-> 索引」和「哪条不用应答」。

索引来源
--------
名字 -> 索引 全部来自 tools/hash_rpc_names.py（genRpcIndex 哈希预言机），
可用 hash_rpc_names.py verify 重新核对，不靠记忆。
"""
from . import ms as msmod

# ---------------------------------------------------------------------------
# 阶段 1 三个组件的 serverRpc 清单（名字 -> genRpcIndex）
# ---------------------------------------------------------------------------
# 出处：entities/avatarcomponent/avatarcharactercomp.lua / avatarbagcomp.lua /
#       avataruserdatacomp.lua 里所有 serverRpc("...") 调用点。
RPC_INDEX = {
    # --- 编队 avatarformationcomp（4 条，2026-10-20 用 genRpcIndex 现算） ---
    # 切换角色走 SetChoosenIndex_CS / SetChoosenChar_CS；
    # 以前它们不在索引表里，落进通用 ACK（只回 status=true 不改数据），
    # 所以「切换之后还是导演模型」。
    "SetChoosenIndex_CS": 1556161069,
    "SetChoosenChar_CS": 524477307,
    "SetCharacterList_CS": 811745113,
    "SetCharacterListName_CS": 414235078,
    # ★ P1 阶段 N-a1：取名链路（namecomp.lua:7-36）。
    #   历史：这 3 个曾经都缺，导致 player_name 永远是默认值 -> Sign 事件不发
    #   -> TaskData[10] 永不完成 -> TaskData[1] 永不进包
    #   -> 40 条 unlockType=1 的系统入口数据条件永假。2026-10-22 已补齐，勿删。
    "RpcCheckNickName": 586398124,
    "RpcChangeNickName": 1344002661,
    "RpcCommitNameGender": 288358305,
    # --- 街区切换 avatarstreetcomp（3 条） ---
    # 客户端发完会自己 JumpToStreetStage 加载场景，但小地图按服务端
    # street_id 取图，所以必须改属性并推同步（见 _switch_street 注释）。
    "RequestSwitchStreet_CS": 87385605,
    "RpcEnterStreet": 831308625,
    "RequestLeaveStreet_CS": 1707898947,
    # --- 剧情任务 avatarstorytaskcomp / avatarstreetcomp（3 条） ---
    "RequestDoneStoryTaskParamInt_CS": 544079713,
    # ★ 短信系统（avatarnpcchatcomp.lua）。索引来自 docs/RPC总表.md:275,457,772
    #   （tools/gen_rpc_list.py 生成的 genRpcIndex 真值，不许手改）。
    "Check_TopicID_CanOpen_CS": 1770853395,
    "Update_NpcChat_Info_CS": 924340932,
    "GmRpcOpenNpcChatTopic": 2096911602,
    # ★ P1 阶段 J：战斗内 SpaceTask 完成上报。
    #   依据 battlecontainer/base/player.lua:416-452 ——
    #   SpaceTask 完成时客户端发 FinishSpaceTask_Directly_CS {taskId}；
    #   服务端按 SpaceTaskData[taskId].avatarTask 完成对应剧情任务。
    #   这是 270 条 TaskType=0 剧情任务**唯一**的完成路径（见 docs 阶段 J）。
    "FinishSpaceTask_Directly_CS": 964136682,
    "RequestDoneStoryTaskParamStr_CS": 1517361152,
    "RequestStreetActor_CS": 200553260,
    # --- 通用任务 avatartaskcomp（5 条） ---
    "RequestFetchTask_CS": 1744435905,
    "RequestDoneTask_CS": 42514969,
    "RequestCompleteTask_CS": 1977280315,
    "RequestCompleteUnitlTask_CS": 697814155,
    "RequestDoneSomeTypeTask_CS": 2089931997,
    # --- 空间/场景 avatarspacecomp（3 条，RpcEnterSpace/RpcOnSceneLoaded 是单向） ---
    "RpcLeaveSpace": 377940221,
    # --- 街景交互 / 红点 / 追踪（3 条） ---
    "Street_Actor_InteractID_Update_CS": 1693572640,
    "Street_Redpoint_Update_CS": 1315049093,
    "updateStreetTraceTask_CS": 1390116544,
    # --- 阴阳界 / 宝箱（2 条，另外两条是单向） ---
    "RequestEnterStreetSurface_CS": 743475140,
    "RequestStreetTreasure_CS": 1398226462,
    # --- 主线等级 / 剧情线 / 伊甸聊天（3 条） ---
    "Street_Mainline_Levelup_CS": 484169195,
    "finishStreetStoryline_CS": 1586165443,
    "RpcGetEdenChatHistory": 1469599847,
    # --- 街景声望（3 条） ---
    "RequestAddPrestigeExp_CS": 685191554,
    # 新手引导进度上报（客户端 avatarsystemguidecomp.lua:371-378，索引取自客户端 rpc_list）
    "System_Guide_Update_CS": 324175358,
    # 送礼（好感度）：client avatarfavorspacecomp.lua:230；索引取自客户端 rpc_list.tsv
    "AddCharFavorExp_CS": 949853662,
    "RequestGetPrestigeAward_CS": 980877859,
    "RequestGetPrestigeTaskAward_CS": 285611839,
    # --- 街景派遣（3 条） ---
    "RequestDispatchChar_CS": 358282292,
    "RequestFinishDispatch_CS": 538869841,
    "RequestCancelDispatchChar_CS": 515348959,
    # --- 剩余街景（3 条，另外 3 条是单向） ---
    "RequestUpgradeDungeonMainPower_CS": 1367222224,
    "RequestGetStreetLevelAchieveAward_CS": 196124224,
    "Street_CD_Update_CS": 338297034,
    # --- 称号任务（2 条，凑满 task 组 7 条） ---
    # avatartaskcomp.lua:208-226：两条 SC 都只看 ret.status，服务端回成功即可
    "RequestRefreshTitleDailyTask_CS": 1068584076,
    "RequestDoneTitleTask_CS": 719776379,
    # --- 单向消息（serverMsg / CallServer，rid=0） ---
    # 这些不带回调，服务端只改数据不回包，处理器在 ONE_WAY_HANDLERS 里。
    # 也登记进索引表，方便统计和覆盖率核对。
    "RequestUpdateStreetPos_CS": 294469585,
    "RpcEnterSpace": 1277075552,
    "RpcOnSceneLoaded": 996547331,
    "RpcEnterEden": 1588890197,
    "RpcBackToEden": 1369842456,
    "RpcOnClientMove": 1920167376,
    "RpcChangeMicroState": 1517096639,
    "RequestClearStreetTreasureActor_CS": 1675033651,
    "RequestLeaveStreetSurface_CS": 2137482665,
    "RequestTouchTreasure_CS": 1282426820,
    "RpcPlayActEmoji": 2000707256,
    "RpcEnterTargetSeq": 2099437863,
    "Cmd_Avatar_StatEnterBattleFialed_CS": 2031265256,
    "Cmd_LoadingProcessSync_CS": 790742624,
    "soulHeartbeat_CS": 1768525703,
    "Cmd_Property_ReliableSync_CS": 1136123026,
    "spaceFrameMessage": 1640580279,
    "spaceRpcMessage": 577431259,
    "defineRpcMessageToLogic": 780255370,
    "sendDungeonMsgToServer": 670252998,
    "Space_CallSpaceMethod_CS": 1740981687,
    "continueSendMsg_CS": 1909403499,
    "RpcSoulAckReliableMsg": 1062918872,
    "Cmd_Soul_WatchOtherPlayer_CS": 531534451,
    "sendDDZMessage": 1936154067,
    "sendMahjongMessage": 739755462,
    "earlyLeaveDDZ": 649611053,
    "earlyLeaveMahjong": 404472060,
    # 带回调的只剩这一条
    "RollbackSpaceTask_CS": 1803698937,
    # --- 副本结算（推进剧情 TaskType 3/7） ---
    # 注：Cmd_Avatar_StartGame_CS 在下面的「登录链路」组里登记（同值，去重后只留一处）
    "Cmd_OfflineSpace_Finish_CS": 2006240685,
    "Storyline_FinishGame": 855259445,
    # --- 副本内关卡切换 ---
    "Cmd_Avatar_EnterNext_CS": 43868592,
    "Cmd_Avatar_FinishGame_CS": 1846356571,
    # 普通副本入口（审计发现之前完全没接，导致 321/322 个副本进不去）
    "SingleStartMatch_CS": 1996394131,
    # --- P0.5：副本里高频出现、之前回「未知索引」的 ---
    # 注：RecordItemObtain_CS 在 1-2 背包组登记（同值，去重后只留一处）
    "BattleTeach_Activate_CS": 842596900,
    "Cmd_Dungeon_FinishInteract_CS": 1040600642,
    "Cmd_Infinite_FinishInteract_CS": 390634949,
    # --- 1-1 角色数据模型 avatarcharactercomp（9 条） ---
    "Cmd_Avatar_Skin_Scheme_CS": 621179191,
    "Cmd_Avatar_Skin_Scheme_Choose_CS": 449377134,
    "Cmd_Avatar_Unlock_SkinColor_CS": 26472811,
    "Cmd_Avatar_Unlock_SkinScheme_CS": 348369310,
    "UpgradeCharacter_CS": 732022161,
    "BreakCharacter_CS": 1158350933,
    "FetchBreakAward_CS": 88532432,
    "RisingStarCharacter_CS": 923231355,
    "Cmd_Avatar_UseCharSkin_CS": 202012670,
    # --- 1-2 背包与货币 avatarbagcomp（4 条） ---
    "UseItem_CS": 168315907,
    "RecordItemObtain_CS": 638405549,
    "SubmitItem_CS": 180596169,
    "RpcGiftCodeRedeem": 1880463470,
    # --- 1-3 玩家数据 avataruserdatacomp（22 条） ---
    "SetCharShown_CS": 253387242,
    "SetShownHonorWallCharId_CS": 1408482175,
    "SetPortrait_CS": 40460550,
    "SetPlayerTitle_CS": 381810662,
    "SetPlayerCard_CS": 1015071836,
    "SetPlayerCardFrame_CS": 68979491,
    "SetPortraitFrame_CS": 473613168,
    "SetBackground_CS": 287510254,
    "SetChatBubble_CS": 1765824800,
    "SetIsAccountCreateTimeHidden_CS": 1937445410,
    "SetIsCharListShown_CS": 1959195014,
    "SetIsHonorWallShown_CS": 1213546920,
    "SetSignature_CS": 2040954396,
    "SetCancelScore_CS": 1450356294,
    "SetHasScore_CS": 927394016,
    "RetSetHasScore_CS": 84440019,
    "SetCanScore_CS": 885846933,
    "ForbidSelf_CS": 1919248075,
    "RpcSetTextHideState": 1392832367,
    "RpcQueryExpiredFashions": 1945701179,
    "QueryExpiredItems_CS": 1811387902,
    "UpgradePlayerStage_CS": 1476365309,
    # --- 登录链路已经在用的（0-4 阶段确认过） ---
    "RpcSyncServerTime": msmod.RPC_SYNC_SERVER_TIME,
    "RpcPullHotfix": msmod.RPC_PULL_HOTFIX,
    "RpcGetSunActLevel_CS": msmod.RPC_GET_SUNACT_LEVEL_CS,
    "RpcGetMatchDuration": msmod.RPC_GET_MATCH_DURATION,
    "QueryMyTeamInfo_CS": msmod.RPC_QUERY_MY_TEAM_INFO_CS,
    "networkCheckAfterChange_CS": msmod.RPC_NETWORK_CHECK,
    "Cmd_Avatar_StartGame_CS": msmod.RPC_CMD_AVATAR_STARTGAME_CS,
}

RPC_NAME_BY_INDEX = {v: k for k, v in RPC_INDEX.items()}

# 单向通知：客户端用的是 serverMsg / CallServer，rid 恒为 0，不该应答。
# 依据：basecomp.lua:8 CallServer("RpcPullHotfix", ...)、
#       avatarnetcomp.lua:29/60 serverMsg("networkCheckAfterChange_CS", ...)
ONE_WAY = {
    "RpcPullHotfix",
    "networkCheckAfterChange_CS",
    "RpcSyncServerTime",       # 要回，但回的是 entityMessage 的 RpcOnSyncServerTime
}

# 统一成功应答。
# 依据：avatarbagcomp.SubmitItem_SC(ret) -> if ret.status；
#       avatarcharactercomp.Cmd_Avatar_Skin_Scheme_SC -> if retStatus.status；
#       avataruserdatacomp 里所有 Set*_SC 同理。
ACK_OK = {"status": True, "errmsg": ""}


def name_of(index):
    return RPC_NAME_BY_INDEX.get(index)


def describe(index):
    nm = name_of(index)
    return nm or (f"未知索引 {index}")
