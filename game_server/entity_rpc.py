#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实体 RPC（serverRpc）应答层。

为什么需要这一层
----------------
客户端 entities/avatarcomponent/avatarrpccomp.lua:17-50 的 _sendServerRpc：

    local turnCircleTime = 2
    local disposable = self:addTimer(turnCircleTime, function()
      ui.postEvent(EUIEventDefine.EStartWeakNetTurnCircle)
    end)
    ...
    self.server:serverRpc(name, parameters, responseCB)

也就是说 **任何一个 serverRpc 只要 2 秒内没收到应答，客户端就弹弱网转圈，
接着 DisconnectionPanel 走重新登录**。所以服务端必须对每条带 rid 的实体
RPC 都回一个 entityMessageResponse。

应答长什么样
------------
core/client/gateclientrpchandler.lua:128-150：

    function GateClientRpcHandler:entityMessageResponse(rid, parameters)
      local name, callback = ClientRepo.responseManager:popResponseCallback(rid)
      if callback then
        local retStatus, response = unpack(parameters)
        SafeCallback(callback, retStatus, response)
      end
    end

所以 parameters 是 [retStatus, response] 两元组，帧类型 3。
retStatus 只需要有 status 字段 —— 三个组件里所有 _SC 处理函数第一句都是
if retstatus.status then ...（avatarbagcomp / avatarcharactercomp /
avataruserdatacomp 逐个确认过），只有极少数还读 errorcode / color。

索引来源
--------
名字 -> 索引 全部来自 tools/hash_rpc_names.py（genRpcIndex 哈希预言机），
可用 hash_rpc_names.py verify 重新核对，不靠记忆。
"""
import struct

import time

from . import ms as msmod
from . import config as ooconf
from . import gamedata
from . import story
from . import tasks

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
    # --- 街区切换 avatarstreetcomp（3 条） ---
    # 客户端发完会自己 JumpToStreetStage 加载场景，但小地图按服务端
    # street_id 取图，所以必须改属性并推同步（见 _switch_street 注释）。
    "RequestSwitchStreet_CS": 87385605,
    "RpcEnterStreet": 831308625,
    "RequestLeaveStreet_CS": 1707898947,
    # --- 剧情任务 avatarstorytaskcomp / avatarstreetcomp（3 条） ---
    "RequestDoneStoryTaskParamInt_CS": 544079713,
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
    # 这些不带回调，服务端只改数据不回包，处理器在 _ONE_WAY_HANDLERS 里。
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
    "Cmd_OfflineSpace_Finish_CS": 2006240685,
    "Cmd_Avatar_StartGame_CS": 136701728,
    "Storyline_FinishGame": 855259445,
    # --- 副本内关卡切换 ---
    "Cmd_Avatar_EnterNext_CS": 43868592,
    "Cmd_Avatar_FinishGame_CS": 1846356571,
    # --- P0.5：副本里高频出现、之前回「未知索引」的 ---
    "BattleTeach_Activate_CS": 842596900,
    "RecordItemObtain_CS": 638405549,
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
    return nm if nm else ("未知索引 %s" % index)


# ---------------------------------------------------------------------------
# 具体应答
# ---------------------------------------------------------------------------
def _sunact_level(rid, params, uid=None):
    """RpcGetSunActLevel_CS -> RpcGetSunActLevel_SC。

    avatarsunactcomp.lua:315-321：
      RpcGetSunActLevel_SC(ret, response) -> self.sun_daily_level_ids = response.levelIds
    没有周常数据就给空表，客户端 response.levelIds 拿到 {}，索引得到 nil，
    avatarsunactcomp.lua:226 的 if not self.sun_daily_level_ids 也放行。
    """
    return msmod.build_sunact_level_sc(rid)


def _match_duration(rid, params, uid=None):
    """RpcGetMatchDuration -> 应答。

    avatarmatch.lua:73-79：
      self:serverRpc("RpcGetMatchDuration", {}, function(retstatus, response)
        if retstatus.status then self.predictMatchTime = response.data or {} end
      end)
    """
    return msmod.build_match_duration_sc(rid)


def _team_info(rid, params, uid=None):
    """QueryMyTeamInfo_CS -> 应答（该玩家不在任何队伍里）。

    avatarteamcomp.lua:688-707：
      QueryMyTeamInfo_SC(cb, retstatus, response)：
        status 为假 -> self:ResetTeamInfo()，cb(false)
      CheckTeamStatus -> TeamErrorResult(nil, nil)：errcode=-1 查不到
      TeamErrorOrient，安静返回，不会弹错误提示。
    """
    return msmod.build_team_info_sc(rid)


def _upgrade_character(rid, params, uid=None):
    """1-1 角色升级：加 CharLevel + 推 characterBag 子属性同步。

    客户端 entities/avatarcomponent/avatarcharcomp.lua:1015-1029：
      UpgradeCharacter(charId, level, items)
        -> serverRpc("UpgradeCharacter_CS", {charId, sendItems}, ...)
      UpgradeCharacter_SC(charId, addLevel, retstatus, response)
        -> CheckCharacterStatus 且 addLevel > 0 时 post ECharLevelUp

    等级读取（同文件 237-261）：
      getCharacterLevel(charID) -> characterBag[charID]["CharLevel"] or 1
    所以把 CharLevel 写进角色字典，再推子属性同步即可。

    characterBag 结构（initDict 实测）：
      {__tp__:"CharacterPersistentDict", __id__:1326, 9001:{__id__:1341, useSkinId:..., ...}}
    键在 _conv_int_keys 之后是整数 9001；但 proto 的 name 字段是字符串，
    所以这里传 str(key)，客户端 _changeSubProperty 里会 tonumber 回来。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    char_id = vals[0]
    st = _state(uid)
    bag = st.get("characterBag")
    if not isinstance(bag, dict):
        return frames
    bag_id = bag.get("__id__") or 0
    key = None
    if char_id in bag:
        key = char_id
    elif str(char_id) in bag:
        key = str(char_id)
    elif isinstance(char_id, str) and char_id.isdigit() and int(char_id) in bag:
        key = int(char_id)
    if key is None:
        return frames
    char = bag.get(key)
    if not isinstance(char, dict):
        return frames
    try:
        old_level = int(char.get("CharLevel") or 1)
    except Exception:
        old_level = 1
    char["CharLevel"] = old_level + 1
    char.setdefault("CharExp", 0)
    st.set("characterBag", bag)
    st.save()
    frames.append(msmod.build_property_sync(
        "", bag_id, msmod.OP_CHANGE, str(key), char))
    return frames


def _set_character_list(rid, params, uid=None):
    """编队内容：SetCharacterList_CS {idx, characterList} / SetCharacterListName_CS {index, name}。

    依据 entities/avatarcomponent/avatarformationcomp.lua:128-146：
      SetCharacterList_CS(idx, characterList)   characterList 是角色 id 列表
      SetCharacterListName_CS(index, name)

    character_list 结构（initDict）：
      {__tp__:"CharacterList", __id__:1340,
       1:{__id__:1354, name:"", value:{__id__:1356, 1:9001, 2:0, 3:0}}}
    改第 idx 队属于「改子属性」：propertyId = character_list 的 __id__，
    name = 队伍下标，value = 整队对象。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    idx = vals[0]
    st = _state(uid)
    cl = st.get("character_list")
    if not isinstance(cl, dict):
        return frames
    team = cl.get(idx)
    if not isinstance(team, dict) and isinstance(idx, int):
        team = cl.get(str(idx))
    if not isinstance(team, dict):
        return frames

    lst = team.get("value")
    if not isinstance(lst, dict):
        lst = {"__id__": st.next_property_id()}
        team["value"] = lst
    for k in [k for k in lst if isinstance(k, int)]:
        del lst[k]
    if isinstance(vals[1], list):
        for i, cid in enumerate(vals[1], 1):
            try:
                lst[i] = int(cid)
            except Exception:
                lst[i] = 0
    st.set("character_list", cl)
    st.save()
    frames.append(msmod.build_property_sync(
        "", cl.get("__id__") or 0, msmod.OP_CHANGE, str(idx), team))
    return frames


def _set_character_list_name(rid, params, uid=None):
    """SetCharacterListName_CS {index, name}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    idx, name = vals[0], vals[1]
    st = _state(uid)
    cl = st.get("character_list")
    if not isinstance(cl, dict):
        return frames
    team = cl.get(idx)
    if not isinstance(team, dict) and isinstance(idx, int):
        team = cl.get(str(idx))
    if not isinstance(team, dict):
        return frames
    team["name"] = name if isinstance(name, str) else ""
    st.set("character_list", cl)
    st.save()
    frames.append(msmod.build_property_sync(
        "", cl.get("__id__") or 0, msmod.OP_CHANGE, str(idx), team))
    return frames


# ---------------------------------------------------------------------------
# 街区切换：RequestSwitchStreet_CS / RpcEnterStreet / RequestLeaveStreet_CS
# ---------------------------------------------------------------------------
# 逆向依据
# --------
# entities/avatarcomponent/avatarstreetcomp.lua:107-128, 651-668：
#   enterStreet_CS(id)        -> serverRpc("RpcEnterStreet", {id, streetParams})
#   AvatarStreet_SwitchStreet -> serverRpc("RequestSwitchStreet_CS", {streetId, streetParams})
#   leaveStreet_CS(id)        -> serverRpc("RequestLeaveStreet_CS", {id})
#
# 客户端拿到 status=true 后会自己 JumpToStreetStage(StreetData, streetId) 加载场景，
# **但小地图是按服务端属性取的**：
#   ui/panel/mappanel/node_map_small/node_map_smallgroup.lua:30
#     DataTable("Common", "StreetData")[GlobalData.Avatar:getStreetID()]
#   entities/avatarcomponent/avatarstreetcomp.lua:24  getStreetID() -> self.street_id
# 所以服务端必须改 street_id 并推同步，否则场景对了、地图还是旧街区。
#
# 另外 street_info 是 StreetInfoDict（generated/config.lua:6834/6928），
# 客户端多处 getStreetInfo() 直接取 self.street_info[street_id]，nil 会崩，
# 所以切换时要保证目标街区有条目。

_STREET_INFO_DEFAULTS = {
    "prestige_street_id": 0,
    "prestige_level": 0,
    "prestige_exp": 0,
    "surface_switch_enter_time": 0,
    "surface_switch_can_enter_time": 0,
    "cd_choose": 0,
}


def _ensure_street_info(st, street_id):
    """确保 street_info[street_id] 存在，返回 (street_info, entry)。

    条目字段照 generated/config.lua:6834 的 StreetInfo 声明；
    四个 IntDictKeyInt 子表（prestige_awarded / prestige_tasks /
    treasure_box_info / inter_actor_info）和 exploration_info 留空即可 ——
    客户端读它们时都做了 `or {}` 兜底。
    """
    si = st.get("street_info")
    if not isinstance(si, dict):
        return None, None
    cur = si.get(street_id)
    if isinstance(cur, dict):
        return si, cur
    entry = {"__tp__": "StreetInfo", "__id__": st.next_property_id()}
    entry.update(_STREET_INFO_DEFAULTS)
    # 嵌套的自定义属性必须带 __id__（不带 __tp__），否则客户端 init 报错
    for _k in ("exploration_info", "prestige_awarded", "prestige_tasks",
               "treasure_box_info", "inter_actor_info"):
        entry[_k] = {"__id__": st.next_property_id()}
    si[street_id] = entry
    st.set("street_info", si)
    return si, entry


def _switch_street(rid, params, uid=None):
    """RequestSwitchStreet_CS {streetId, streetParams} / RpcEnterStreet {id, streetParams}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        new_id = int(vals[0])
    except Exception:
        return frames
    return _apply_street_change(frames, new_id, uid)


def _leave_street(rid, params, uid=None):
    """RequestLeaveStreet_CS {id} —— 离开街区，street_id 归 0。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    return _apply_street_change(frames, 0, uid)


def _apply_street_change(frames, new_id, uid):
    """改 street_id + 保证 street_info 有条目 + 推两条同步。"""
    st = _state(uid)
    old = st.props.get("street_id")
    st.props["street_id"] = new_id
    st.save()
    frames.append(msmod.build_property_sync(
        "", 0, msmod.OP_CHANGE, "street_id", new_id))
    if new_id:
        si, entry = _ensure_street_info(st, new_id)
        if si is not None and entry is not None:
            frames.append(msmod.build_property_sync(
                "", si.get("__id__") or 0, msmod.OP_CHANGE, str(new_id), entry))
    print("[street] %s -> %s" % (old, new_id), flush=True)
    return frames


# ---------------------------------------------------------------------------
# 剧情任务推进（阶段 2 核心）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstorytaskcomp.lua:
#   getStoryTaskState(taskID)  读 story_task_bag[taskID].state / done_story_tasks
#   getTaskIdByParamInt        遍历 story_task_bag 找 TaskType+Parameter1 命中的
#   RequestDoneStoryTaskParamInt_CS {taskId, param_int}
#   RequestDoneStoryTaskParamStr_CS {taskId, param_str}
#   RequestStreetActor_CS {actor_id}  = RequestDoneStoryTaskParamInt_CS(2, actor_id)
#     + serverRpc("RequestStreetActor_CS")  ← 街景 NPC 交互就是剧情推进


def _story_sync_frames(st, changes):
    """把 story 模块的同步描述转成 propertySync 帧。"""
    frames = []
    for ch in changes:
        kind = ch[0]
        if kind == "root":
            _, name, value = ch
            frames.append(msmod.build_property_sync(
                "", 0, msmod.OP_CHANGE, name, value))
        elif kind == "add":
            # ★ 给自定义 dict **加新键**必须用 OP_ADD（客户端 __OpAdd__），
            #   用 OP_CHANGE 会走 __OpChange__，那里 oldVal 为 nil 直接报
            #   "subproperty is nil" 并把这次变更丢掉。
            #   实测症状：剧情任务完成后 story_task_bag 删掉了，
            #   但 done_story_tasks 里没有 -> 客户端 getTaskStateByTaskID 返回 -1。
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(msmod.build_property_sync(
                "", c.get("__id__") or 0, msmod.OP_ADD, str(key), value))
        elif kind == "sub":
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(msmod.build_property_sync(
                "", c.get("__id__") or 0, msmod.OP_CHANGE, str(key), value))
        elif kind == "del":
            _, container, key, _v = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(msmod.build_property_sync(
                "", c.get("__id__") or 0, msmod.OP_DEL, str(key), None))
    return frames


def _recalc_story_unlocks(st):
    """按 AllSystemCatalogueData 的 unlockType=2(StoryTask) 重算系统解锁位。

    客户端判定在 ui/panel/streetv2panel/node_menu_item/node_menu_itemgroup.lua:231：
      unlock = getTaskStateByTaskID(taskId) == Finished
               and isSysEntryUnlock(sysId)
    后者读 system_p.unlocked_system[sysId]，所以服务端两样都要维护。
    """
    frames = []
    sp = st.props.get("system_p")
    if not isinstance(sp, dict):
        return frames
    us = sp.get("unlocked_system")
    if not isinstance(us, dict):
        return frames
    us_id = us.get("__id__") or 0
    changed = []
    for row in gamedata.all_system_catalogue():
        if row.get("unlockType") != 2:
            continue
        tid = row.get("unlockParam")
        sid = row.get("id")
        if not isinstance(tid, int) or not isinstance(sid, int):
            continue
        if us.get(sid) is True:
            continue
        if story.story_state(st.props, tid) != story.TASK_STATE_FINISHED:
            continue
        us[sid] = True
        changed.append(sid)
    if changed:
        st.set("system_p", sp)
        for sid in changed:
            frames.append(msmod.build_property_sync(
                "", us_id, msmod.OP_CHANGE, str(sid), True))
        print("[story] 解锁系统 %s" % changed, flush=True)
    return frames


def _finish_story_tasks(st, task_ids):
    """完成若干剧情任务，返回全部同步帧。"""
    frames = []
    now = int(time.time())
    for tid in task_ids:
        if story.story_state(st.props, tid) == story.TASK_STATE_FINISHED:
            continue
        changes = story.finish_story_task(st, tid, now)
        frames.extend(_story_sync_frames(st, changes))
        # 发奖励（新格子用 OP_ADD，已有格子用 OP_CHANGE）
        for award in story.story_award(st, tid):
            _item_id, _cnt, grid_key, is_new = award
            bag = st.props.get("item_bag")
            if not isinstance(bag, dict):
                break
            grid = bag.get(grid_key)
            if isinstance(grid, dict):
                frames.append(msmod.build_property_sync(
                    "", bag.get("__id__") or 0,
                    msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                    str(grid_key), grid))
        print("[story] 完成剧情任务 %s" % tid, flush=True)
    frames.extend(_recalc_story_unlocks(st))
    st.save()
    return frames


def _done_story_task(rid, params, uid=None):
    """RequestDoneStoryTaskParamInt_CS / ParamStr_CS —— 参数第 0 项是 taskId。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    frames.extend(_finish_story_tasks(st, [tid]))
    return frames


def _street_actor(rid, params, uid=None):
    """RequestStreetActor_CS {actor_id} —— 街景 NPC 交互。

    客户端 avatarstreetcomp.lua:339-342 先调
      RequestDoneStoryTaskParamInt_CS(2, actor_id)
    再调 serverRpc("RequestStreetActor_CS", {actor_id})。
    这里按同样的规则找命中任务：story_task_bag 里 TaskType==2 且 Parameter1 含 actor_id。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        actor_id = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    hits = _find_story_tasks_by_param(st, actor_id, 2)
    if hits:
        frames.extend(_finish_story_tasks(st, hits))
    return frames


def _find_story_tasks_by_param(st, param_int, task_type):
    """照 getTaskIdByParamInt 的规则找任务。"""
    out = []
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        return out
    for tid, item in bag.items():
        if not isinstance(item, dict):
            continue
        cfg = gamedata.story_task(tid) or {}
        if cfg.get("TaskType") != task_type:
            continue
        p1 = cfg.get("Parameter1") or []
        if param_int in p1:
            out.append(int(tid))
    return out


# ---------------------------------------------------------------------------
# 通用任务（TaskData 那一套：task_bag / done_tasks）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatartaskcomp.lua:
#   getTaskState(taskType, taskID) 读 task_bag[taskType][taskID] / done_tasks[taskType][taskID]
#   RequestFetchTask_CS {task_id}          接任务
#   RequestDoneTask_CS {task_id}           完成任务
#   RequestCompleteTask_CS {task_id}       完成任务（另一条路径）
#   RequestCompleteUnitlTask_CS {task_id}  完成到指定任务
#   RequestDoneSomeTypeTask_CS {type}      完成某类型的全部任务
#
# 注意 task_bag 是**两层**：task_bag[taskType] 是 TaskBag（Dict<taskId, TaskItem>），
# 所以同步要打在 TaskBag 的 __id__ 上，name = taskId。


def _task_sync_frames(st, changes):
    """把 tasks 模块的同步描述转成帧（支持两层嵌套 sub2/del2）。"""
    frames = []
    for ch in changes:
        kind = ch[0]
        if kind in ("sub2", "del2"):
            _, container, type_key, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            slot = c.get(type_key)
            if not isinstance(slot, dict):
                continue
            op = msmod.OP_CHANGE if kind == "sub2" else msmod.OP_DEL
            frames.append(msmod.build_property_sync(
                "", slot.get("__id__") or 0, op, str(key),
                value if kind == "sub2" else None))
    return frames


def _task_fetch(rid, params, uid=None):
    """RequestFetchTask_CS {task_id} —— 接任务（放进 task_bag）。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    frames.extend(_task_sync_frames(st, tasks.start_task(st, tid)))
    st.save()
    print("[task] 接任务 %s" % tid, flush=True)
    return frames


def _task_done(rid, params, uid=None):
    """RequestDoneTask_CS / RequestCompleteTask_CS / RequestCompleteUnitlTask_CS。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    frames.extend(_task_sync_frames(st, tasks.finish_task(st, tid)))
    st.save()
    print("[task] 完成任务 %s" % tid, flush=True)
    return frames


def _task_done_type(rid, params, uid=None):
    """RequestDoneSomeTypeTask_CS {type} —— 把该类型所有进行中任务标完成。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        ttype = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    for tid in tasks.tasks_of_type(st, ttype):
        frames.extend(_task_sync_frames(st, tasks.finish_task(st, tid)))
    st.save()
    print("[task] 完成类型 %s 的全部任务" % ttype, flush=True)
    return frames


# ---------------------------------------------------------------------------
# 空间/场景进出（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarspacecomp.lua：
#   EnterSpace(spaceno, spaceid, pos, yaw, info)
#     -> CallServer("RpcEnterSpace", spaceno, spaceid or "", info)   单向
#   LeaveSpace() -> CallServer("RpcLeaveSpace")                       带回调
#   OnSceneLoaded(spaceno) -> CallServer("RpcOnSceneLoaded", spaceno) 单向
#
# 服务端要做两件事：
#   1. 改 space_p.spaceno / seqno / spaceid 并推属性同步（客户端 IsInHotSpace 读它）
#   2. **主动下发** ServerOnlyMsg RpcOnEnterSpace(spaceno, spaceid, info)，
#      客户端 RpcOnEnterSpace 里才会去 JumpToEdenStage / OnSceneLoaded 加载场景
#
# space_p 结构（initDict）：
#   {__tp__:SpaceProp, __id__:1200, spaces:{__id__:1201},
#    spaceno:0, seqno:0, spaceid:"", loading:false, ltime:0, ld_spaceno:0}
#
# SpaceConst.SPACE_EDEN = 1000（gamecommon/consts/spaceconst.lua:2）

SPACE_EDEN = 1000

# ServerOnlyMsg 的索引（genRpcIndex 现算，见 work/rpc_hashes.tsv）
RPC_ON_ENTER_SPACE = 1879036833      # RpcOnEnterSpace(int, string, table)
RPC_ON_ENTER_SPACE_FAILED = 0        # 未算，用到再补
RPC_ON_TELEPORT_OUT = 354358832      # RpcOnTeleportOut(int)


def _space_prop(st):
    sp = st.props.get("space_p")
    if not isinstance(sp, dict):
        return None
    return sp


def _space_sync_frames(st, sp):
    """把 space_p 的 spaceno/seqno/spaceid 推给客户端。"""
    frames = []
    sp_id = sp.get("__id__") or 0
    for name in ("spaceno", "seqno", "spaceid", "loading", "ld_spaceno"):
        frames.append(msmod.build_property_sync(
            "", sp_id, msmod.OP_CHANGE, name, sp.get(name)))
    return frames


def _s2c_entity(method_name, index, params):
    """构造一条服务端 -> 客户端的 entityMessage。"""
    return msmod.build_entity_message(
        entity_id="", method_name=method_name, index=index,
        params=msmod.mp_arr(params) if isinstance(params, list) else params)


def _enter_space(params, uid=None):
    """RpcEnterSpace [spaceno, spaceid, info] —— 进/出空间。

    spaceno == 0 表示回到普通世界（离开空间）。
    """
    # params 可能是 msgpack 字节（来自客户端），也可能是已经解好的 list
    # （服务端内部调用，比如 _enter_eden 直接传 [SPACE_EDEN, ...]）。
    vals = params if isinstance(params, list) else decode_params(params)
    if not isinstance(vals, list):
        vals = []
    try:
        spaceno = int(vals[0]) if len(vals) > 0 and vals[0] is not None else 0
    except Exception:
        spaceno = 0
    spaceid = vals[1] if len(vals) > 1 and isinstance(vals[1], str) else ""
    info = vals[2] if len(vals) > 2 and isinstance(vals[2], dict) else {}
    st = _state(uid)
    sp = _space_prop(st)
    if sp is None:
        return []
    old = int(sp.get("spaceno") or 0)
    sp["spaceno"] = spaceno
    sp["seqno"] = int(sp.get("seqno") or 0) + 1
    sp["spaceid"] = spaceid
    sp["loading"] = bool(spaceno)
    sp["ld_spaceno"] = 0
    st.set("space_p", sp)
    st.save()
    print("[space] %s -> %s (spaceid=%s)" % (old, spaceno, spaceid), flush=True)
    frames = _space_sync_frames(st, sp)
    if spaceno:
        frames.append(_s2c_entity(
            "RpcOnEnterSpace", RPC_ON_ENTER_SPACE,
            [spaceno, spaceid, info]))
    elif old:
        frames.append(_s2c_entity(
            "RpcOnTeleportOut", RPC_ON_TELEPORT_OUT, [old]))
    return frames


def _scene_loaded(params, uid=None):
    """RpcOnSceneLoaded [spaceno] —— 客户端场景加载完成。"""
    vals = decode_params(params)
    if not isinstance(vals, list) or not vals:
        return []
    try:
        spaceno = int(vals[0])
    except Exception:
        return []
    st = _state(uid)
    sp = _space_prop(st)
    if sp is None:
        return []
    sp["loading"] = False
    sp["ld_spaceno"] = spaceno
    st.set("space_p", sp)
    st.save()
    print("[space] 场景加载完成 %s" % spaceno, flush=True)
    return _space_sync_frames(st, sp)


def _leave_space_rpc(rid, params, uid=None):
    """RpcLeaveSpace —— 离开空间（带回调，需要回应答）。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    st = _state(uid)
    sp = _space_prop(st)
    if sp is None:
        return frames
    old = int(sp.get("spaceno") or 0)
    sp["spaceno"] = 0
    sp["seqno"] = int(sp.get("seqno") or 0) + 1
    sp["spaceid"] = ""
    sp["loading"] = False
    st.set("space_p", sp)
    st.save()
    print("[space] 离开空间 %s" % old, flush=True)
    frames.extend(_space_sync_frames(st, sp))
    if old:
        frames.append(_s2c_entity(
            "RpcOnTeleportOut", RPC_ON_TELEPORT_OUT, [old]))
    return frames


# ---------------------------------------------------------------------------
# 伊甸（Eden）与 AOI 移动（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avataredencomp.lua:30-39：
#   enterEden(pos_id)  -> CallServer("RpcEnterEden", pos_id or 0)   单向
#   BackToEden()       -> CallServer("RpcBackToEden")               单向
# 两者最终都是「进 SPACE_EDEN 空间」：服务端改 space_p 并下发
#   RpcOnEnterSpace(1000, spaceid, {toPosId=...})
# 客户端 avatarspacecomp.lua:93-118 的 RpcOnEnterSpace 里会
#   JumpToEdenStage("StreetData", 502, ...) 加载伊甸场景。
#
# AOI（entities/avatarcomponent/avataraoicomp.lua:7）：
#   CallServer("RpcOnClientMove", moveInfo.x, moveInfo.y, moveInfo.z, moveInfo.dir)
# 单向，记录玩家位置即可（多人联机时要广播给同场景其他人）。


def _enter_eden(params, uid=None):
    """RpcEnterEden [pos_id] / RpcBackToEden [] —— 进伊甸空间。"""
    vals = decode_params(params)
    pos_id = 0
    if isinstance(vals, list) and vals:
        try:
            pos_id = int(vals[0] or 0)
        except Exception:
            pos_id = 0
    info = {}
    if pos_id:
        info["toPosId"] = pos_id
    return _enter_space([SPACE_EDEN, "", info], uid)


def _client_move(params, uid=None):
    """RpcOnClientMove [x, y, z, dir] —— 记录位置。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 3):
        return []
    st = _state(uid)
    try:
        saved = {"x": float(vals[0]), "y": float(vals[1]), "z": float(vals[2]),
                 "dir": float(vals[3]) if len(vals) > 3 and vals[3] is not None else 0.0}
    except Exception:
        return []
    st.props["_oo_street_pos"] = saved
    _throttled_save(st, uid)
    return []


def _change_micro_state(params, uid=None):
    """RpcChangeMicroState [state] —— 记录微状态（挂机/钓鱼等）。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return []
    st = _state(uid)
    try:
        st.props["_oo_micro_state"] = int(vals[0] or 0)
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# 街景交互 / 红点 / 追踪任务（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetactorcomp.lua:19-40：
#   sendStreetActorHadInteract(actor_id, interact_id, interact_group, cb)
#     -> serverRpc("Street_Actor_InteractID_Update_CS",
#                   {actor_id, interact_group, interact_id, taskId, interact_id})
#   语义：把「这个 NPC 的这个交互做过了」记到服务端，
#         若命中剧情任务（getTaskIdByParamInt(interact_id, 2)）则顺便完成它。
#
# 交互状态存在哪：StreetInfo 的 inter_actor_info（IntDictKeyInt），
# 见 generated/config.lua:6834 StreetInfo.Properties —— actorId -> interactId。
#
# entities/avatarcomponent/avatarstreetcomp.lua:581-587：
#   saveRedPointCount(key, value)
#     -> serverRpc("Street_Redpoint_Update_CS", {key, value})
#   存在 StreetInfoDict.street_redpoint（IntDict）。
#
# entities/avatarcomponent/avatarstreetcomp.lua:741：
#   updateStreetTraceTask_CS(trackingTaskID) -> 存 street_trace_taskid


def _street_info_of(st, street_id):
    """取 street_info[street_id]，没有就建。"""
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return None, None
    cur = si.get(street_id)
    if isinstance(cur, dict):
        return si, cur
    entry = {"__tp__": "StreetInfo", "__id__": st.next_property_id()}
    entry.update(_STREET_INFO_DEFAULTS)
    for _k in ("exploration_info", "prestige_awarded", "prestige_tasks",
               "treasure_box_info", "inter_actor_info"):
        entry[_k] = {"__id__": st.next_property_id()}
    si[street_id] = entry
    st.set("street_info", si)
    return si, entry


def _actor_interact(rid, params, uid=None):
    """Street_Actor_InteractID_Update_CS {actor_id, interact_group, interact_id, taskId?, interact_id}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 3):
        return frames
    try:
        actor_id = int(vals[0])
        interact_id = int(vals[2])
    except Exception:
        return frames
    task_id = None
    if len(vals) >= 4:
        try:
            task_id = int(vals[3])
        except Exception:
            task_id = None
    st = _state(uid)
    street_id = int(st.props.get("street_id") or 0)
    si, entry = _street_info_of(st, street_id)
    if entry is not None:
        ia = entry.get("inter_actor_info")
        if not isinstance(ia, dict):
            ia = {"__id__": st.next_property_id()}
            entry["inter_actor_info"] = ia
        ia[actor_id] = interact_id
        st.set("street_info", si)
        frames.append(msmod.build_property_sync(
            "", si.get("__id__") or 0, msmod.OP_CHANGE, str(street_id), entry))
    print("[street] NPC %s 交互 %s" % (actor_id, interact_id), flush=True)
    # 命中剧情任务就顺手完成
    if task_id:
        frames.extend(_finish_story_tasks(st, [task_id]))
    else:
        hits = _find_story_tasks_by_param(st, interact_id, 2)
        if hits:
            frames.extend(_finish_story_tasks(st, hits))
    st.save()
    return frames


def _street_redpoint(rid, params, uid=None):
    """Street_Redpoint_Update_CS {key, value} —— 存在 street_info.street_redpoint。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        key = int(vals[0])
        value = int(vals[1])
    except Exception:
        return frames
    st = _state(uid)
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return frames
    rp = si.get("street_redpoint")
    if not isinstance(rp, dict):
        rp = {"__id__": st.next_property_id()}
        si["street_redpoint"] = rp
    rp[key] = value
    st.set("street_info", si)
    st.save()
    frames.append(msmod.build_property_sync(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, "street_redpoint", rp))
    return frames


def _street_trace_task(rid, params, uid=None):
    """updateStreetTraceTask_CS {trackingTaskID} —— 存追踪中的任务 id。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0] or 0)
    except Exception:
        tid = 0
    st = _state(uid)
    st.props["street_trace_taskid"] = tid
    st.save()
    frames.append(msmod.build_property_sync(
        "", 0, msmod.OP_CHANGE, "street_trace_taskid", tid))
    return frames


def _clear_treasure_actor(params, uid=None):
    """RequestClearStreetTreasureActor_CS {streetID} —— 单向，清掉该街区的宝箱 actor。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return []
    try:
        street_id = int(vals[0])
    except Exception:
        return []
    st = _state(uid)
    si, entry = _street_info_of(st, street_id)
    if entry is not None:
        tb = entry.get("treasure_box_info")
        if isinstance(tb, dict):
            for k in [k for k in tb if isinstance(k, int)]:
                del tb[k]
            st.set("street_info", si)
    print("[street] 清理街区 %s 的宝箱 actor" % street_id, flush=True)
    return []


# ---------------------------------------------------------------------------
# 阴阳界切换 + 街景宝箱（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:433-448, 328-334：
#   RequestEnterStreetSurface(worldId) -> serverRpc("RequestEnterStreetSurface_CS", {worldId})
#   RequestLeaveStreetSurface()        -> serverMsg("RequestLeaveStreetSurface_CS", {})  单向
#   RequestTouchTreasure_CS(treasure_id)  -> serverMsg（单向）
#   RequestStreetTreasure_CS(treasure_id) -> serverRpc
#
# 阴阳界状态读的是 **StreetInfoDict.surface_enter_time**（不是每街区的）：
#   avatarstreetcomp.lua:560-562
#     function AvatarStreetComp:getCurStreetYinYangEnterTime()
#       return self.street_info.surface_enter_time or 0
#     end
#   而 streetsystemmgrbase.lua:679-685 用它决定 worldIndex（0=阳界 1=阴界）。
#
# 宝箱状态在 StreetInfo.treasure_box_info（IntDictKeyInt），
#   avatarstreetcomp.lua:353-363 checkTreasureIsComplete 读它（0 < state 即已开）。


def _set_surface_time(st, value):
    """改 street_info.surface_enter_time 并推同步。"""
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return []
    si["surface_enter_time"] = int(value)
    st.set("street_info", si)
    return [msmod.build_property_sync(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, "surface_enter_time", int(value))]


def _enter_surface(rid, params, uid=None):
    """RequestEnterStreetSurface_CS {worldId} —— 进阴界。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    st = _state(uid)
    frames.extend(_set_surface_time(st, int(time.time())))
    st.save()
    print("[street] 进入阴界", flush=True)
    return frames


def _leave_surface(params, uid=None):
    """RequestLeaveStreetSurface_CS {} —— 回阳界（单向）。"""
    st = _state(uid)
    out = _set_surface_time(st, 0)
    st.save()
    print("[street] 离开阴界", flush=True)
    return out


def _grant_treasure_reward(st, tid):
    """按 StreetTreasureTaskData[tid] 发宝箱奖励，返回同步帧。

    逆向依据（BDD 实测，44 条宝箱配置）：
      AwardItemList = {道具id: 数量}      —— 38/44 条有
      AwardAttr     = {street_id, prestige_exp}  —— 30/44 条有（都是 +6 声望经验）

    之前只把 treasure_box_info[tid] 标成 1，**奖励一个没发** ——
    玩家点开箱子只看到开箱动画，没道具也没声望，就是这个原因。
    """
    frames = []
    cfg = gamedata.street_treasure(tid)
    if not isinstance(cfg, dict):
        return frames
    # 1) 道具奖励
    items = cfg.get("AwardItemList") or {}
    bag = st.props.get("item_bag")
    if isinstance(items, dict) and isinstance(bag, dict):
        for k, v in items.items():
            if k in ("KeyList", "__bdd_metatype__"):
                continue
            try:
                iid = int(k)
                cnt = int(v)
            except Exception:
                continue
            if cnt <= 0:
                continue
            got = story.add_item(st.props, iid, cnt)
            if not got:
                continue
            num, gkey, is_new = got
            grid = bag.get(gkey)
            if isinstance(grid, dict):
                frames.append(msmod.build_property_sync(
                    "", bag.get("__id__") or 0,
                    msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                    str(gkey), grid))
            print("[street] 宝箱 %s 发道具 %s x%s -> 现在 %s"
                  % (tid, iid, cnt, num), flush=True)
    # 2) 声望经验（AwardAttr.prestige_exp）
    aa = cfg.get("AwardAttr") or {}
    exp = 0
    sid = None
    if isinstance(aa, dict):
        try:
            exp = int(aa.get("prestige_exp") or 0)
            sid = aa.get("street_id")
        except Exception:
            exp = 0
    if exp > 0:
        target = int(sid) if sid else int(st.props.get("street_id") or 0)
        si2, entry2, s2 = _prestige_info(st, target)
        if entry2 is not None:
            frames.extend(_apply_prestige_exp(
                st, si2, entry2, s2, exp, save=False))
    return frames


def _touch_treasure(params, uid=None):
    """RequestTouchTreasure_CS {treasure_id} / RequestStreetTreasure_CS {treasure_id}。

    1. 标记该宝箱已开：StreetInfo.treasure_box_info[treasure_id] = 1
    2. **按 StreetTreasureTaskData 发奖励**（道具 + 声望经验）
    """
    vals = params if isinstance(params, list) else decode_params(params)
    if not (isinstance(vals, list) and vals):
        return None
    try:
        tid = int(vals[0])
    except Exception:
        return None
    st = _state(uid)
    street_id = int(st.props.get("street_id") or 0)
    si, entry = _street_info_of(st, street_id)
    if entry is None:
        return None
    tb = entry.get("treasure_box_info")
    if not isinstance(tb, dict):
        tb = {"__id__": st.next_property_id()}
        entry["treasure_box_info"] = tb
    already = tb.get(tid) == 1
    tb[tid] = 1
    st.set("street_info", si)
    print("[street] 开宝箱 %s（街区 %s）%s"
          % (tid, street_id, "重复开启，不再发奖" if already else ""), flush=True)
    frames = [msmod.build_property_sync(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, str(street_id), entry)]
    if not already:
        frames.extend(_grant_treasure_reward(st, tid))
    st.save()
    return frames


def _street_treasure_rpc(rid, params, uid=None):
    """RequestStreetTreasure_CS {treasure_id} —— 带回调版本。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    out = _touch_treasure(params, uid)
    if out:
        frames.extend(out)
    return frames


# ---------------------------------------------------------------------------
# 主线关卡等级 / 街区剧情线 / 伊甸聊天记录（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetcomp.lua：
#   Street_Mainline_Levelup_CS(area_id) -> serverRpc({area_id})   :261-263
#     读侧 getMainLineLevel(area_id) = self.mainline_level[area_id]  :265-268
#     mainline_level 是 IntDictKeyInt（我们已补进 initDict）
#   finishStreetStoryline_CS {eventId}                             :752-757
#     标记某条街区剧情线完成
#   avatarspacecomp.lua:147-158：
#     GetEdenChatHistory(spaceNo) -> CallServer("RpcGetEdenChatHistory", spaceNo)
#     客户端回调 RpcOnEdenChatHistory(chatInfo) 里取数据


def _mainline_levelup(rid, params, uid=None):
    """Street_Mainline_Levelup_CS {area_id} —— 主线关卡等级 +1。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        area_id = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    ml = st.props.get("mainline_level")
    if not isinstance(ml, dict):
        return frames
    cur = int(ml.get(area_id) or 0)
    ml[area_id] = cur + 1
    st.set("mainline_level", ml)
    st.save()
    print("[street] 主线 %s 等级 %s -> %s" % (area_id, cur, cur + 1), flush=True)
    frames.append(msmod.build_property_sync(
        "", ml.get("__id__") or 0, msmod.OP_CHANGE, str(area_id), cur + 1))
    return frames


def _finish_storyline(rid, params, uid=None):
    """finishStreetStoryline_CS {eventId} —— 记录完成的街区剧情线事件。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        event_id = int(vals[0])
    except Exception:
        return frames
    st = _state(uid)
    done = st.props.get("_oo_storyline_done")
    if not isinstance(done, list):
        done = []
    if event_id not in done:
        done.append(event_id)
    st.props["_oo_storyline_done"] = done
    st.save()
    print("[street] 街区剧情线完成 event=%s" % event_id, flush=True)
    return frames


def _eden_chat_history(rid, params, uid=None):
    """RpcGetEdenChatHistory {spaceNo} —— 回一条空聊天记录。

    客户端 avatarspacecomp.lua:152-158 的 RpcOnEdenChatHistory(chatInfo)
    只是把 chatInfo 交给回调，空表即可。
    """
    resp = {"Ret": [True, 0], "ChatInfo": []}
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK), resp=resp)]
    return frames


# ---------------------------------------------------------------------------
# 街景声望（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:
#   getPrestigePrize_CS(streetId, level) -> RequestGetPrestigeAward_CS {streetId, level}  :137-139
#   addPrestigeExp_CS(exp)               -> RequestAddPrestigeExp_CS {exp}               :141-143
#   getPrestigeTaskAward_CS(streetId, prestigeTaskId)                                    :99-101
#
# 数据在 StreetInfo（generated/config.lua:6834）：
#   prestige_street_id  声望归属街区
#   prestige_level      当前声望等级
#   prestige_exp        当前等级内的经验
#   prestige_awarded    IntDictKeyInt，已领过的等级奖励标记为 1
#   prestige_tasks      IntDictKeyInt，声望任务状态
# 升级阈值在 BDD StreetPrestigeData[streetId]（每条有 needExpNum / level / levelAward）。


def _prestige_info(st, street_id=None):
    """取（或建）当前街区的 StreetInfo，并保证声望字段齐全。"""
    if street_id is None:
        street_id = int(st.props.get("street_id") or 0)
    si, entry = _street_info_of(st, street_id)
    if entry is None:
        return None, None, None
    if not isinstance(entry.get("prestige_awarded"), dict):
        entry["prestige_awarded"] = {"__id__": st.next_property_id()}
    if not isinstance(entry.get("prestige_tasks"), dict):
        entry["prestige_tasks"] = {"__id__": st.next_property_id()}
    if not entry.get("prestige_street_id"):
        entry["prestige_street_id"] = street_id
    return si, entry, street_id


def _prestige_push(st, si, street_id, entry):
    return [msmod.build_property_sync(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, str(street_id), entry)]


def _apply_prestige_exp(st, si, entry, street_id, add, save=True):
    """给某街区加声望经验并处理升级，返回同步帧。

    宝箱奖励（StreetTreasureTaskData.AwardAttr.prestige_exp）和
    声望 RPC（RequestAddPrestigeExp_CS）共用这段逻辑。
    """
    frames = []
    if add <= 0 or entry is None:
        return frames
    entry["prestige_exp"] = int(entry.get("prestige_exp") or 0) + int(add)
    rows = gamedata.street_prestige(
        entry.get("prestige_street_id") or street_id)
    by_level = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("level"), int):
            by_level[row["level"]] = int(row.get("needExpNum") or 0)
    lv = int(entry.get("prestige_level") or 0)
    exp = int(entry["prestige_exp"])
    while True:
        need = by_level.get(lv + 1)
        if need is None or exp < need:
            break
        exp -= need
        lv += 1
    entry["prestige_level"] = lv
    entry["prestige_exp"] = exp
    st.set("street_info", si)
    if save:
        st.save()
    print("[street] 声望 +%s -> level=%s exp=%s" % (add, lv, exp), flush=True)
    frames.extend(_prestige_push(st, si, street_id, entry))
    return frames


def _add_prestige_exp(rid, params, uid=None):
    """RequestAddPrestigeExp_CS {exp} —— 加声望经验，够了就升级。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        add = int(vals[0] or 0)
    except Exception:
        return frames
    if add <= 0:
        return frames
    st = _state(uid)
    si, entry, street_id = _prestige_info(st)
    if entry is None:
        return frames
    frames.extend(_apply_prestige_exp(st, si, entry, street_id, add))
    return frames


def _get_prestige_award(rid, params, uid=None):
    """RequestGetPrestigeAward_CS {streetId, level} —— 领声望等级奖励。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        street_id = int(vals[0] or 0)
        level = int(vals[1] or 0)
    except Exception:
        return frames
    st = _state(uid)
    si, entry, sid = _prestige_info(st, street_id)
    if entry is None:
        return frames
    entry["prestige_awarded"][level] = 1
    st.set("street_info", si)
    st.save()
    print("[street] 领声望奖励 street=%s level=%s" % (street_id, level), flush=True)
    frames.extend(_prestige_push(st, si, sid, entry))
    return frames


def _get_prestige_task_award(rid, params, uid=None):
    """RequestGetPrestigeTaskAward_CS {streetId, prestigeTaskId} —— 领声望任务奖励。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        street_id = int(vals[0] or 0)
        task_id = int(vals[1] or 0)
    except Exception:
        return frames
    st = _state(uid)
    si, entry, sid = _prestige_info(st, street_id)
    if entry is None:
        return frames
    entry["prestige_tasks"][task_id] = 1
    st.set("street_info", si)
    st.save()
    print("[street] 领声望任务奖励 street=%s task=%s" % (street_id, task_id), flush=True)
    frames.extend(_prestige_push(st, si, sid, entry))
    return frames


# ---------------------------------------------------------------------------
# 街景派遣（阶段 2）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:277-300：
#   RequestDispatchChar_CS {char_id, dip_id}    派角色去打工
#   RequestFinishDispatch_CS {dip_id}           完成（领收益）
#   RequestCancelDispatchChar_CS {dip_id}       取消
#   getDispacthData(dip_id) = self.street_dispatch[dip_id]
#
# street_dispatch 是 StreetDispatchDict（Dict<int, StreetDispatchInfo>），
# 客户端靠 entryAdded/entryDeleted/changed 三个回调刷新 UI，
# 所以新增/删除都要推对应 optype：
#   新增 -> OP_ADD（对应 entryAdded）
#   删除 -> OP_DEL（对应 entryDeleted）
#   修改 -> OP_CHANGE（对应 changed）
#
# StreetDispatchInfo（generated/config.lua）：
#   street_id / dispatch_id / char_id / beg_time / end_time

_DISPATCH_DEFAULT_DURATION = 3600


def _dispatch_dict(st):
    d = st.props.get("street_dispatch")
    if not isinstance(d, dict):
        return None
    return d


def _dispatch_char(rid, params, uid=None):
    """RequestDispatchChar_CS {char_id, dip_id}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        char_id = int(vals[0] or 0)
        dip_id = int(vals[1] or 0)
    except Exception:
        return frames
    st = _state(uid)
    d = _dispatch_dict(st)
    if d is None:
        return frames
    now = int(time.time())
    entry = {
        "__tp__": "StreetDispatchInfo",
        "__id__": st.next_property_id(),
        "street_id": int(st.props.get("street_id") or 0),
        "dispatch_id": dip_id,
        "char_id": char_id,
        "beg_time": now,
        "end_time": now + _DISPATCH_DEFAULT_DURATION,
    }
    d[dip_id] = entry
    st.set("street_dispatch", d)
    st.save()
    print("[street] 派遣 char=%s dip=%s" % (char_id, dip_id), flush=True)
    frames.append(msmod.build_property_sync(
        "", d.get("__id__") or 0, msmod.OP_ADD, str(dip_id), entry))
    return frames


def _finish_dispatch(rid, params, uid=None):
    """RequestFinishDispatch_CS {dip_id} —— 完成派遣，把 end_time 提前到 now。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        dip_id = int(vals[0] or 0)
    except Exception:
        return frames
    st = _state(uid)
    d = _dispatch_dict(st)
    if d is None or not isinstance(d.get(dip_id), dict):
        return frames
    entry = d[dip_id]
    entry["end_time"] = int(time.time())
    st.set("street_dispatch", d)
    st.save()
    print("[street] 完成派遣 dip=%s" % dip_id, flush=True)
    frames.append(msmod.build_property_sync(
        "", d.get("__id__") or 0, msmod.OP_CHANGE, str(dip_id), entry))
    return frames


def _cancel_dispatch(rid, params, uid=None):
    """RequestCancelDispatchChar_CS {dip_id} —— 取消派遣。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        dip_id = int(vals[0] or 0)
    except Exception:
        return frames
    st = _state(uid)
    d = _dispatch_dict(st)
    if d is None or dip_id not in d:
        return frames
    del d[dip_id]
    st.set("street_dispatch", d)
    st.save()
    print("[street] 取消派遣 dip=%s" % dip_id, flush=True)
    frames.append(msmod.build_property_sync(
        "", d.get("__id__") or 0, msmod.OP_DEL, str(dip_id), None))
    return frames


def _play_act_emoji(params, uid=None):
    """RpcPlayActEmoji [emoji_id] —— AOI 表情广播，单机无别人，记录即可。"""
    vals = decode_params(params)
    if isinstance(vals, list) and vals:
        try:
            _state(uid).props["_oo_last_emoji"] = int(vals[0] or 0)
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# 剩余街景 / 空间 RPC（阶段 2 收尾）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstreetcomp.lua：
#   Street_RequestUpgradeDungeonMainPower_CS -> RequestUpgradeDungeonMainPower_CS {}   :699-707
#     SC 只看 ret.status
#   RequestGetStreetLevelAchieveAward_CS {level_id, achieve_id}                       :765-778
#     SC 只看 ret.status（失败弹「领取失败」）
#   Street_CD_Update_CS {street_id, street_cd_id}                                     :149-154
#     SC 回调 responseCb(status)
#   avatarspacecomp.lua:60  RpcEnterTargetSeq(spaceno, seqno, info)                   单向
#   Cmd_Avatar_StatEnterBattleFialed_CS                                               单向（战斗进入失败统计）
#   Cmd_LoadingProcessSync_CS                                                         单向（加载进度）


def _ack_only(rid, params, uid=None):
    """只回 status=true 的 RPC。"""
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK))]


def _street_cd_update(rid, params, uid=None):
    """Street_CD_Update_CS {street_id, street_cd_id} —— 记录当前选的 CD 并回成功。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        street_id = int(vals[0] or 0)
        cd_id = int(vals[1] or 0)
    except Exception:
        return frames
    st = _state(uid)
    si, entry = _street_info_of(st, street_id)
    if entry is not None:
        entry["cd_choose"] = cd_id
        st.set("street_info", si)
        st.save()
        frames.append(msmod.build_property_sync(
            "", si.get("__id__") or 0, msmod.OP_CHANGE, str(street_id), entry))
    print("[street] CD 选择 street=%s cd=%s" % (street_id, cd_id), flush=True)
    return frames


def _noop_one_way(params, uid=None):
    """单向无操作（RpcEnterTargetSeq / 战斗失败统计 / 加载进度）。"""
    return []


# ---------------------------------------------------------------------------
# Soul / 战斗帧同步（阶段 2 收尾，Phase 3 才真正跑起来）
# ---------------------------------------------------------------------------
# 逆向依据（都是 spaceentities/soulcomponent 下的组件）：
#   soulnet.lua:76              soulHeartbeat_CS {{reqMs}, pingInfo}  单向
#     -> 服务端必须回 soulHeartbeat_SC {reqMs}，否则 15 秒后客户端判定超时并断 Soul
#   soulpropertyreliablesync.lua:34  Cmd_Property_ReliableSync_CS {msgId, data}  单向
#     -> 回 Cmd_Property_ReliableSync_SC(msgId) 停止重发
#   onlinebattlespacenetcomponent.lua:138  spaceFrameMessage {view2LogicDataCache}
#     -> 回 syncFrameFromServerToClient(帧数据)（单机时原样回传即可）
#   spaceRpcMessage / defineRpcMessageToLogic / sendDungeonMsgToServer /
#   Space_CallSpaceMethod_CS / continueSendMsg_CS / RpcSoulAckReliableMsg /
#   Cmd_Soul_WatchOtherPlayer_CS / sendDDZMessage / sendMahjongMessage /
#   earlyLeaveDDZ / earlyLeaveMahjong  —— 全是单向，服务端记录即可
#   soulspacetask.lua:195/209  RollbackSpaceTask_CS  带回调
#
# ★ 这些消息都走 `localSoul:serverMsg(...)`，需要 Soul 实体已绑定；
#   而 bindSoul 依赖副本入口（Phase 3）。现在先把 RPC 面补全。

SOUL_HEARTBEAT_SC = 1531962635
CMD_PROPERTY_RELIABLE_SYNC_SC = 66862165
SYNC_FRAME_FROM_SERVER = 1791975984


def _soul_heartbeat(params, uid=None):
    """soulHeartbeat_CS {{reqMs}, pingInfo} -> soulHeartbeat_SC {reqMs}。

    客户端 soulnet.lua:82-90 用 `now - info.reqMs` 算 ping，
    并在 :82-89 里做 15 秒超时判定 —— 不回就会断 Soul 连接。
    """
    vals = decode_params(params)
    req = 0
    if isinstance(vals, list) and vals:
        first = vals[0]
        if isinstance(first, dict):
            req = int(first.get("reqMs") or 0)
        elif isinstance(first, (int, float)):
            req = int(first)
    return [_s2c_entity("soulHeartbeat_SC", SOUL_HEARTBEAT_SC,
                        [{"reqMs": req}])]


def _reliable_property_sync(params, uid=None):
    """Cmd_Property_ReliableSync_CS {msgId, data} -> Cmd_Property_ReliableSync_SC(msgId)。"""
    vals = decode_params(params)
    msg_id = 0
    if isinstance(vals, list) and vals:
        try:
            msg_id = int(vals[0] or 0)
        except Exception:
            msg_id = 0
    return [_s2c_entity("Cmd_Property_ReliableSync_SC",
                        CMD_PROPERTY_RELIABLE_SYNC_SC, [msg_id])]


def _space_frame_message(params, uid=None):
    """spaceFrameMessage {view2LogicDataCache} -> syncFrameFromServerToClient(帧)。

    单机私服没有别的玩家，把客户端上行的帧原样回传，
    让战斗逻辑走完「上行 -> 服务端确认 -> 下行」的闭环。
    """
    vals = decode_params(params)
    payload = []
    if isinstance(vals, list) and vals:
        payload = vals[0]
    return [_s2c_entity("syncFrameFromServerToClient", SYNC_FRAME_FROM_SERVER,
                        [payload])]


# ---------------------------------------------------------------------------
# 副本结算 -> 推进剧情（TaskType 3/7）
# ---------------------------------------------------------------------------
# 逆向依据：
#   entities/avatarcomponent/avatardungeoncomp.lua:217-222
#     FinishDungeonSpace(finishInfo) -> serverRpc("Cmd_OfflineSpace_Finish_CS", {finishInfo})
#   entities/avatarcomponent/avatardungeoncomp.lua:203-215
#     Cmd_Dungeon_FinishInteract_CS {} / Cmd_Infinite_FinishInteract_CS {}
#
# 剧情推进链条（BDD 实测）：
#   StoryTask 1000 (TaskType=3, Parameter1=[1])
#     -> TaskData[1] {Description:"完成序章", TriggerEventList:[4001]}
#     -> TaskEventData[4001] {EventType:"FinishDungeon",
#                             EventParam:{typeId:1004, modeId:1, level:1}}
#   所以副本结算时把 finishInfo 里的整数键都当事件参数喂给 story.evaluate_event，
#   命中就完成对应剧情任务。


# SystemType.MainStory = 1004（序章就是这个）
_STORY_TYPE_MAINSTORY = 1004


def _resolve_copy_id(match_info):
    """按 match_info 解析出 Level_CopyData 的 copyId。"""
    if not isinstance(match_info, dict):
        return None
    st = match_info.get("system_type")
    # MainStory: StoryLineData[storyID].dungeonId
    if st == _STORY_TYPE_MAINSTORY:
        sid = match_info.get("storyID") or match_info.get("storyId")
        line = gamedata.story_line(sid)
        if isinstance(line, dict) and line.get("dungeonId"):
            return line["dungeonId"]
        return None
    # 其它系统：先看调用方有没有直接给 copyId / dungeonId
    for k in ("copyId", "dungeonid", "dungeonId"):
        v = match_info.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _avatar_start_game(rid, params, uid=None):
    """Cmd_Avatar_StartGame_CS {match_info} —— 副本入口，回 spaceInfo。"""
    st = _state(uid)
    vals = decode_params(params)
    match_info = vals[0] if (isinstance(vals, list) and vals
                             and isinstance(vals[0], dict)) else {}
    if not isinstance(match_info, dict):
        match_info = {}
    copy_id = _resolve_copy_id(match_info)
    if not copy_id:
        print("[dungeon] 解析不出 copyId，match_info=%s" % match_info, flush=True)
        return [msmod.build_entity_response(
            rid, ret={"status": False, "errorcode": 1})]
    copy_cfg = gamedata.level_copy(copy_id) or {}
    roots = copy_cfg.get("rootLevel") or []
    # ★ 注意嵌套：客户端 SpaceUtils.enterOnlineHub(stage, data, offlineSpace)
    #   读的是 data.spaceInfo.copyId / data.spaceInfo.levelIndex，
    #   而 Storyline_StartGame_Callback 传进去的是 resp.spaceInfo。
    #   所以 RPC 应答的 spaceInfo 字段里还要再套一层 spaceInfo。
    #   踩过：只给一层时客户端报
    #     SpaceUtils.lua:226 attempt to index field 'spaceInfo' (a nil value)
    inner = {
        "copyId": copy_id,
        "levelIndex": 1,          # getCopy 是 1-based
        "systemType": match_info.get("system_type"),
        "match_info": match_info,
    }
    if roots:
        ids = [int(x) for x in roots if isinstance(x, int)]
        # 客户端 dispatchhubonline.lua:49-59 优先用服务端给的 levelIds，
        # 所以覆盖关卡列表就能直接进指定关卡（跳过前面的关卡）。
        # ★ 按账号的关卡覆盖（存玩家存档的 _oo_ 私有字段，客户端看不到）。
        #   之前是全局的，多开/换号时所有人都会被塞进同一关 —— 那是错的。
        force = 0
        try:
            force = int(st.props.get("_oo_dungeon_force_level") or 0)
        except (TypeError, ValueError):
            force = 0
        if force:
            if force in ids:
                ids = [force]
                print("[dungeon] 强制关卡 %s（%s 的账号设置）" % (force, uid),
                      flush=True)
            else:
                print("[dungeon] ⚠ 强制关卡 %s 不属于本副本（%s），忽略"
                      % (force, ids), flush=True)
        elif ooconf.DUNGEON_LAST_ONLY and len(ids) > 1:
            # 命令行 --dungeon-last-only：只下发最后一关
            ids = ids[-1:]
            print("[dungeon] --dungeon-last-only：只下发最后一关 %s" % ids,
                  flush=True)
        inner["levelIds"] = ids
    space_info = {"spaceInfo": inner, "battleInfo": {}}
    st.props["_oo_cur_copy"] = copy_id
    # 结算时要用它反查 typeId/modeId 推进剧情（客户端 finishInfo 里没有）
    st.props["_oo_cur_match"] = match_info
    st.save()
    print("[dungeon] 进副本 copyId=%s roots=%s" % (copy_id, roots), flush=True)
    resp = {"status": True, "spaceInfo": space_info}
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK), resp=resp)]


def _storyline_finish_game(rid, params, uid=None):
    """Storyline_FinishGame {storyID, passed, useTime} —— 标记剧情段落完成。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        sid = int(vals[0] or 0)
    except Exception:
        return frames
    passed = True
    if len(vals) > 1:
        passed = bool(vals[1])
    if not passed:
        return frames
    st = _state(uid)
    sf = st.props.get("story_finished")
    if not isinstance(sf, dict):
        return frames
    if sf.get(sid) is not True:
        sf[sid] = True
        st.set("story_finished", sf)
        st.save()
        frames.append(msmod.build_property_sync(
            "", sf.get("__id__") or 0,
            msmod.OP_CHANGE if sid in sf else msmod.OP_ADD,
            str(sid), True))
    print("[dungeon] 剧情段落 %s 完成" % sid, flush=True)
    return frames


# ---------------------------------------------------------------------------
# 副本内关卡切换（阶段 3）
# ---------------------------------------------------------------------------
# ★ 逆向依据 battlecontainer/dispatchhub/dispatchhubonline.lua:198-232：
#
#   DispatchHubOnline:sendEnterNextLevel(times, levelInfo, copyIndex, ...)
#     if hubPlayer:isOfflineBattle() then
#       GlobalData.Avatar:sendServerRpc("Cmd_Avatar_EnterNext_CS", {copyIndex},
#         CallbackHandler(self, "enterNextLevelCallback", ...))
#     end
#
#   DispatchHubOnline:enterNextLevelCallback(..., ret, response)
#     if ret.status then callback(true, response.levelInfo) end   <-- ★ 需要 levelInfo
#
#   HubPlayer:enterOfflineBattleCallback(copy, copyIndex, playerCreateInfo, result, levelinfo)
#     self.hub:initProgress(levelinfo.levelProgress)   <-- ★ levelinfo 为 nil 直接抛异常
#     self.hub:setTowerLevelInfo(levelinfo.towerLevelInfo, copyIndex)
#     SpaceUtils.fillLevelInitInfo(copy, self.hub.spaceInfo, playerCreateInfo, levelinfo)
#     copy.battleInfo.randSeed = levelinfo.randSeed
#     copy.battleInfo.runTime  = levelinfo.runTime
#
# 实测症状（2026-10-21）：第一关打完切第二关时
#   [Lua] HubPlayer.lua:139: attempt to index local 'levelinfo' (a nil value)
#   下一关的 ServerBattle 根本没建起来，角色在空场景里一直坠落。
#
# 另外 dispatchhubonline.lua:136-151 在离线副本里通关发的是
#   Cmd_Avatar_FinishGame_CS {finishinfo}
# （不是 Soul 上的 Cmd_OfflineSpace_Finish_CS），两条都要接。

RPC_CMD_AVATAR_ENTER_NEXT = 43868592
RPC_CMD_AVATAR_FINISH_GAME = 1846356571


def _avatar_enter_next(rid, params, uid=None):
    """Cmd_Avatar_EnterNext_CS {copyIndex} —— 回下一关的 levelInfo。

    客户端 hubplayer.lua:116-121 会读：
      levelinfo.levelProgress / towerLevelInfo / randSeed / runTime
    少了就是 nil 索引异常，下一关建不起来。
    """
    vals = decode_params(params)
    copy_index = 0
    if isinstance(vals, list) and vals:
        try:
            copy_index = int(vals[0] or 0)
        except Exception:
            copy_index = 0
    st = _state(uid)
    level_info = {
        "levelProgress": {},
        "randSeed": int(time.time()) & 0x7FFFFFFF,
        "runTime": 0,
    }
    print("[dungeon] 进入下一关 copyIndex=%s" % copy_index, flush=True)
    return [msmod.build_entity_response(
        rid, ret=dict(ACK_OK),
        resp={"levelInfo": level_info})]


# ---------------------------------------------------------------------------
# 副本结算（P0，2026-10-21）
# ---------------------------------------------------------------------------
# ★ 完整五步链路（深挖结论，见 ROADMAP 2-4 节）：
#
#   ① 客户端上报 finishInfo（含 toStreetId / toStreetPosId / 每人 endResult）
#        Cmd_Avatar_FinishGame_CS   —— 离线/Avatar 路径（dispatchhubonline.lua:136-151）
#        Cmd_OfflineSpace_Finish_CS —— Soul 路径
#   ② 服务端算奖励
#   ③ 服务端下发 AvatarSpace_SpaceFinish_SC（index 1634212414）  ← 关键缺口
#   ④ 客户端 AvatarSettlementComp:AvatarSpace_SpaceFinish_SC -> OnReceiveEndMsg
#        -> SettleFactory.createSettle() -> normalSettle() -> 结算面板
#   ⑤ 玩家点退出 -> directLeaveBattle -> GoBackToWhere -> 用 toStreetId/toStreetPosId
#        -> JumpToStreetStage 回街区
#
# 我们之前只回了个 ACK，客户端 onFinishGameSuccess(true) 什么都不做
# （offlinebattlespace.lua:217-221），于是卡在战斗空间、输入被锁。
#
# finishInfo 结构（serverbattle.lua:721-758 + logicplayerstaticscomp.lua:128-174）：
#   {
#     space_info = {copyID, collectCount},
#     [uid]      = {win, rank, damage, collectCount, levelCopyIndex,
#                   battle_items = {itemId: 净获得数量},   ← 奖励来源
#                   username, useTime, character_list, ...},
#     userList   = {uid: teamid},
#     gameState, killMonsterInfo, allLevelScores,
#     toStreetId, toStreetPosId, leaveReasonType, finishReason, _leaveSpaceExtInfo,
#   }
#
# 注意：LevelData 里**没有任何奖励字段**，奖励完全来自客户端算好的 battle_items。
#       StoryLineData[storyId].passAward 是剧情线首通奖励，另算。

RPC_AVATAR_SPACE_FINISH_SC = 1634212414   # AvatarSpace_SpaceFinish_SC


def _finish_info_of(info, uid):
    """从 finishInfo 里取出「我」那一份 endResult。"""
    if not isinstance(info, dict):
        return {}
    me = info.get(uid)
    if isinstance(me, dict):
        return me
    for k, v in info.items():
        if isinstance(v, dict) and ("battle_items" in v or "win" in v):
            return v
    return {}


def _grant_battle_items(st, battle_items):
    """把 battle_items 里的道具发进背包，返回 (同步帧列表, reward_item_list)。"""
    frames = []
    reward_list = []
    if not isinstance(battle_items, dict):
        return frames, reward_list
    bag = st.props.get("item_bag")
    if not isinstance(bag, dict):
        return frames, reward_list
    for k, v in battle_items.items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        try:
            iid = int(k)
            cnt = int(v)
        except Exception:
            continue
        if cnt <= 0:
            continue
        got = story.add_item(st.props, iid, cnt)
        if not got:
            continue
        num, gkey, is_new = got
        grid = bag.get(gkey)
        if isinstance(grid, dict):
            frames.append(msmod.build_property_sync(
                "", bag.get("__id__") or 0,
                msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                str(gkey), grid))
        reward_list.append({"item_id": iid, "item_num": cnt})
        print("[dungeon] 结算发道具 %s x%s -> 现在 %s" % (iid, cnt, num),
              flush=True)
    return frames, reward_list


def _settlement_table(st, uid, info, reward_list):
    """构造下发客户端的结算表（AvatarSpace_SpaceFinish_SC 的唯一参数）。

    客户端读的地方：
      avatarsettlementcomp.lua:54-76  remakeResultData
        -> resultData[uid].reward_item_list / useTime / collectCount / win
        -> resultData.userList（遍历找自己）
      avatarsettlementcomp.lua:154-178 GoBackToWhere
        -> data.toStreetId / data.toStreetPosId
      modules/battlesettle/settlefactory.lua:327-335 createSettle
        -> resultData.match_info.system_type（1004 走 normalSettle）
    """
    me = _finish_info_of(info, uid)
    to_street = info.get("toStreetId") if isinstance(info, dict) else None
    to_pos = info.get("toStreetPosId") if isinstance(info, dict) else None
    try:
        to_street = int(to_street) if to_street is not None else None
    except Exception:
        to_street = None
    if not to_street:
        to_street = int(st.props.get("street_id") or 0)
    try:
        to_pos = int(to_pos) if to_pos is not None else 0
    except Exception:
        to_pos = 0
    try:
        finish_reason = int(info.get("finishReason") or 0)
    except Exception:
        finish_reason = 0
    mine = {
        "win": bool(me.get("win", True)),
        "reward_item_list": reward_list,
        "useTime": int(me.get("useTime") or 0),
        "collectCount": int(me.get("collectCount") or 0),
        "rank": int(me.get("rank") or 1),
        "damage": int(me.get("damage") or 0),
    }
    match_info = st.props.get("_oo_cur_match")
    if not isinstance(match_info, dict):
        match_info = {"system_type": 1004, "modeId": 1, "match_type": 3}
    settle = {
        "userList": {uid: 1},
        uid: mine,
        "toStreetId": to_street,
        "toStreetPosId": to_pos,
        "finishReason": finish_reason,
        "match_info": match_info,
    }
    return settle


def _advance_story_on_finish(st, info=None):
    """副本打完后推进剧情。

    优先用 finishInfo 自带的 typeId/modeId/level（兼容手工构造的调用）；
    没有就用 StartGame 时存下的 _oo_cur_match（match_info）反查：
    typeId = system_type，modeId = modeId。
    序章就是 FinishDungeon{typeId:1004, modeId:1, level:1}。
    """
    type_id = 0
    mode_id = 0
    level = 1
    # 1) finishInfo 自带（旧格式 / 测试用）
    if isinstance(info, dict):
        try:
            type_id = int(info.get("typeId") or 0)
            mode_id = int(info.get("modeId") or 0)
            level = int(info.get("level") or 1)
        except Exception:
            type_id = 0
    # 2) 回退到 StartGame 存的 match_info
    if not type_id:
        m = st.props.get("_oo_cur_match")
        if isinstance(m, dict):
            try:
                type_id = int(m.get("system_type") or 0)
                mode_id = int(m.get("modeId") or 0)
            except Exception:
                type_id = 0
    if not type_id:
        return []
    if not type_id:
        return []
    ev = {"typeId": type_id, "modeId": mode_id, "level": level}
    hits, _ch = story.evaluate_event(st, "FinishDungeon", ev)
    out = []
    if hits:
        out.extend(_finish_story_tasks(st, hits))
    # 序章（SystemType.MainStory = 1004）打完 -> story_finished[1] = True，
    # 下次登录 clientavatar.lua:180 的 CheckEnterPrologue 就会跳过序章进街区。
    if type_id == _STORY_TYPE_MAINSTORY:
        sf = st.props.get("story_finished")
        if isinstance(sf, dict) and sf.get(1) is not True:
            is_new = 1 not in sf
            sf[1] = True
            st.set("story_finished", sf)
            out.append(msmod.build_property_sync(
                "", sf.get("__id__") or 0,
                msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                "1", True))
            print("[dungeon] 序章完成，story_finished[1]=True", flush=True)
    chain, _c2 = story.check_chain_tasks(st)
    if chain:
        out.extend(_finish_story_tasks(st, chain))
    if hits:
        print("[dungeon] 结算推进剧情 %s（事件参数 %s）" % (hits, ev), flush=True)
    return out


def _dungeon_finish(rid, params, uid=None):
    """Cmd_Avatar_FinishGame_CS / Cmd_OfflineSpace_Finish_CS —— 副本结算。

    1. 从 finishInfo[uid].battle_items 算奖励并发进背包
    2. 下发 AvatarSpace_SpaceFinish_SC（客户端靠它弹结算面板 + 回街区）
    3. 用 _oo_cur_match 推进剧情
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    info = vals[0] if (isinstance(vals, list) and vals
                      and isinstance(vals[0], dict)) else {}
    if not isinstance(info, dict):
        info = {}
    st = _state(uid)
    me = _finish_info_of(info, uid)
    item_frames, reward_list = _grant_battle_items(
        st, me.get("battle_items") or {})
    frames.extend(item_frames)
    settle = _settlement_table(st, uid, info, reward_list)
    frames.append(msmod.build_entity_message(
        "", "AvatarSpace_SpaceFinish_SC", RPC_AVATAR_SPACE_FINISH_SC,
        msmod.mp_arr([settle])))
    try:
        to_street = int(settle.get("toStreetId") or 0)
    except Exception:
        to_street = 0
    if to_street and to_street != int(st.props.get("street_id") or 0):
        st.props["street_id"] = to_street
        frames.append(msmod.build_property_sync(
            "", 0, msmod.OP_CHANGE, "street_id", to_street))
    frames.extend(_advance_story_on_finish(st, info))
    st.props.pop("_oo_cur_copy", None)
    st.props.pop("_oo_cur_match", None)
    st.save()
    print("[dungeon] 结算完成：奖励 %d 项，回街区 %s 位置 %s"
          % (len(reward_list), settle.get("toStreetId"),
             settle.get("toStreetPosId")), flush=True)
    return frames


def _battle_teach_activate(rid, params, uid=None):
    """BattleTeach_Activate_CS {instructionID} —— 激活战斗教学。

    客户端 avatarsystemguidecomp.lua:406-408 -> serverRpc("BattleTeach_Activate_CS")
    服务端写 battle_teach_state[instructionID] = 0 并推同步：
      config.lua:10084 AvatarSystemGuideComp
        entryAdded.battle_teach_state -> onAddBattleTeach
        changed["battle_teach_state.*"] -> onChangedBattleTeachState
      battle_main_panel.lua:513-516  if newVal ~= 0 then return end
      => 值为 0 才会弹教学 UI
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        iid = int(vals[0] or 0)
    except Exception:
        return frames
    st = _state(uid)
    bts = st.props.get("battle_teach_state")
    if not isinstance(bts, dict):
        return frames
    if iid not in bts:
        bts[iid] = 0
        st.set("battle_teach_state", bts)
        st.save()
        frames.append(msmod.build_property_sync(
            "", bts.get("__id__") or 0, msmod.OP_ADD, str(iid), 0))
        print("[dungeon] 激活战斗教学 %s" % iid, flush=True)
    return frames


def _use_item(rid, params, uid=None):
    """1-2 使用道具：扣数量 + 推 item_bag 的子属性同步。

    客户端 entities/avatarcomponent/avatarbagcomp.lua:340-346：
      UseItem_CS(itemID, itemCount, ext, callback)
        -> serverRpc("UseItem_CS", {itemID, itemCount, ext}, ...)

    同步方式：item_bag 是 CustomDict（initDict 里 __id__ = 1324，
    格子形如 {"1": {__id__: 1357, id: "1", itemId: 101, num: 1000}}）。
    改某个格子的数量属于「改子属性」：
      propertyId = item_bag 的 __id__，name = 格子键，value = 整个格子。
    客户端 _changeSubProperty 里会比对 __id__，命中就原地 rochange。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    item_id = vals[0]
    try:
        count = int(vals[1])
    except Exception:
        return frames
    if count <= 0:
        return frames
    st = _state(uid)
    bag = st.get("item_bag")
    if not isinstance(bag, dict):
        return frames
    bag_id = bag.get("__id__") or 0
    grid_key = None
    grid = None
    for key, g in list(bag.items()):
        if key in ("__tp__", "__id__"):
            continue
        if isinstance(g, dict) and g.get("itemId") == item_id:
            grid_key, grid = key, g
            break
    if grid is None:
        # 背包里没有这个道具：不动数据，只回成功（客户端只看 status）
        return frames

    old_num = grid.get("num") or 0
    if old_num < count:
        return frames

    # ---- 1) 扣数量 ----
    grid["num"] = old_num - count
    st.set("item_bag", bag)

    # ---- 2) 应用道具效果 ----
    # 规则全在 ItemData：useType + useParam。
    # 例：时砂之盒(110) = {useType:"avatarAttr", useParam:{TowerRewardKeys:60}}
    changed_props = _apply_item_effect(st, item_id, count)

    st.save()

    # ---- 3) 推同步 ----
    frames.append(msmod.build_property_sync(
        "", bag_id, msmod.OP_CHANGE, grid_key, grid))
    for prop, value in changed_props:
        frames.append(msmod.build_property_sync(
            "", 0, msmod.OP_CHANGE, prop, value))
    return frames


# useParam 的键（PascalCase）-> 属性名（snake_case）。
# 只列「简单计数器」类的；Portrait/StickerId/Card/Background/Title/
# PortraitFrame/Bubble 这些是往各自的 bag 里加条目，需要 bag 结构，暂未实现。
_USE_PARAM_PROP = {
    "TowerRewardKeys": "tower_reward_keys",
    "Crystal": "crystal",
    "Exp": "exp",
}


def _apply_item_effect(st, item_id, count):
    """应用 ItemData 里声明的道具效果，返回 [(属性名, 新值)]。

    目前只支持 useType == "avatarAttr" 的简单计数器（时砂/龙玉/经验）。
    其它类型（randomItem / char / charSkin / item / selectItem ...）需要
    各自的发放逻辑，属于阶段 2/3 的活。
    """
    try:
        from . import gamedata
    except Exception:
        return []
    use_type = gamedata.item_use_type(item_id)
    if use_type != "avatarAttr":
        return []
    params = gamedata.item_use_param(item_id) or {}
    out = []
    for pk, pv in params.items():
        prop = _USE_PARAM_PROP.get(pk)
        if prop is None:
            continue
        try:
            amount = int(pv) * int(count)
        except Exception:
            continue
        if amount <= 0:
            continue
        if prop == "tower_reward_keys":
            # 体力有上限，走 stamina 模块（会同时更新两个恢复时间戳）
            try:
                from . import stamina as stamina_mod
                lv = int(st.props.get("level") or 1)
                stamina_mod.add(st.props, amount, lv)
                out.append((prop, st.props.get(prop)))
                out.append((stamina_mod.PROP_NEXT, st.props.get(stamina_mod.PROP_NEXT)))
                out.append((stamina_mod.PROP_TOTAL, st.props.get(stamina_mod.PROP_TOTAL)))
                continue
            except Exception as e:
                print("[item] 体力加算失败: %s" % e, flush=True)
        cur = st.props.get(prop)
        if not isinstance(cur, (int, float)):
            continue
        st.props[prop] = cur + amount
        out.append((prop, st.props[prop]))
    return out


# ---------------------------------------------------------------------------
# 1-3 的 Set* 系列：真正改数据 + 推根属性同步
# ---------------------------------------------------------------------------
# 客户端 entities/avatarcomponent/avataruserdatacomp.lua 里每个 setter 都是
#   SetXxx_CS(v, callback) -> serverRpc("SetXxx_CS", {v}, ...)
# 目标属性名对照 SystemConst.CommonUserDataType（common/systemconst.lua:1390）。
#
# 只处理「简单类型 + IntList」的属性：
#   - 简单类型（int/bool/str）：直接改根属性；
#   - IntList 等自定义类型：保留原来的 __tp__/__id__，用新值重建条目，
#     客户端 _changeRootProperty 靠 __id__ 命中做原地 rochange。
# 查表类 RPC（QueryExpiredItems 等）和没有属性可改的（ForbidSelf）只回 ACK。
_SIMPLE_SETTERS = {
    "SetPortrait_CS": ("portrait_id", 0),
    "SetPlayerCard_CS": ("player_card_id", 0),
    "SetPlayerCardFrame_CS": ("player_card_frame_id", 0),
    "SetPortraitFrame_CS": ("portrait_frame_id", 0),
    "SetBackground_CS": ("background_id", 0),
    "SetChatBubble_CS": ("chat_bubble_id", 0),
    "SetIsAccountCreateTimeHidden_CS": ("is_account_create_time_hidden", 0),
    "SetIsCharListShown_CS": ("is_char_list_shown", 0),
    "SetIsHonorWallShown_CS": ("is_honor_wall_shown", 0),
    "SetShownHonorWallCharId_CS": ("shown_honor_wall_char_id", 0),
    "SetSignature_CS": ("signature", 0),
    "UpgradePlayerStage_CS": ("player_stage_id", 0),
    # 编队（entities/avatarcomponent/avatarformationcomp.lua:168-184）
    # 切换角色靠这两条：改 character_choosen_char 后客户端
    # on_character_choosen_char_changed -> EChoosingCharChanged
    # -> stageLogic:switchRole(charId) 才会换模型。
    # 以前这两条走通用 ACK（只回 status=true 不改数据），
    # 所以「切换之后还是导演模型」。
    "SetChoosenIndex_CS": ("choosen_index", 0),
    "SetChoosenChar_CS": ("character_choosen_char", 0),
    "SetCharShown_CS": ("char_list", 0),
    "SetPlayerTitle_CS": ("shown_titles", 0),
}

# 没有参数、只把某个开关置成常量的 setter
_CONST_SETTERS = {
    "SetCanScore_CS": ("canScore", 1),
    "SetHasScore_CS": ("hasScore", 1),
    "RetSetHasScore_CS": ("hasScore", 1),
    "SetCancelScore_CS": ("canScore", 0),
}


def _apply_property(prop_name, new_value, uid=None):
    """把新值写进状态，返回要推给客户端的值。"""
    st = _state(uid)
    cur = st.get(prop_name)
    if isinstance(cur, dict) and "__tp__" in cur:
        # 自定义类型：保留 __tp__/__id__，用新值重建条目
        newobj = {"__tp__": cur["__tp__"], "__id__": cur.get("__id__", 0)}
        if isinstance(new_value, list):
            for i, v in enumerate(new_value, 1):
                newobj["#%d" % i] = v
        st.set(prop_name, newobj)
        st.save()
        return newobj
    st.set(prop_name, new_value)
    st.save()
    return new_value


def _generic_setter(rid, params, spec, uid=None):
    """Set*_CS 的通用处理：改数据 + 推根属性同步。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    prop_name, arg_idx = spec
    if arg_idx is None:
        new_value = spec[2] if len(spec) > 2 else None
    else:
        vals = decode_params(params)
        if not (isinstance(vals, list) and len(vals) > arg_idx):
            return frames
        new_value = vals[arg_idx]
    applied = _apply_property(prop_name, new_value, uid)
    frames.append(msmod.build_property_sync(
        "", 0, msmod.OP_CHANGE, prop_name, applied))
    return frames


def _const_setter(rid, params, spec, uid=None):
    """无参数 setter：把属性置成常量 + 推同步。"""
    prop_name, const_value = spec
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    applied = _apply_property(prop_name, const_value)
    frames.append(msmod.build_property_sync(
        "", 0, msmod.OP_CHANGE, prop_name, applied))
    return frames


def _make_setter(spec, is_const):
    fn = _const_setter if is_const else _generic_setter

    def handler(rid, params, uid=None):
        return fn(rid, params, spec, uid)

    return handler


def _set_signature(rid, params, uid=None):
    """1-3 改签名：真正改服务端状态，再推一条属性同步。

    依据 entities/avatarcomponent/avataruserdatacomp.lua:348-352：
      SetSignature_CS(signature, callback)
        -> serverRpc("SetSignature_CS", {signature}, ...)
    属性同步格式见 ms.build_property_sync（描述符逆向出来的）。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if isinstance(vals, list) and vals and isinstance(vals[0], str):
        st = _state(uid)
        st.set("signature", vals[0])
        st.save()
        # ★ propertyId = 0 表示「改根属性」，客户端会走
        #   puppetpropertycomponent.lua:233-235 的 _changeRootProperty(k, v)。
        #   之前传的是递增序号 1，那是「改子属性」的语义（要 _id 命中
        #   __CustomPropertiesCache__ 里的对象），属于用错了分支。
        frames.append(msmod.build_property_sync(
            "", 0, msmod.OP_CHANGE, "signature", vals[0]))
    return frames


def _state(uid=None):
    """服务端可变玩家状态（按 uid 分开，懒加载，首次用 initDict 初始化）。"""
    from . import player_state as pstate
    # 和 ms._live_init_dict 保持一致：状态里一律用整数键
    props = msmod._conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    return pstate.get_state(props, uid=uid or pstate.DEFAULT_UID)


# 索引 -> 处理函数。没列进来的走通用 ACK。
_HANDLERS = {
    msmod.RPC_INDEX_SET_SIGNATURE_CS: _set_signature,
    msmod.RPC_INDEX_USE_ITEM_CS: _use_item,
    msmod.RPC_INDEX_UPGRADE_CHARACTER_CS: _upgrade_character,
    msmod.RPC_GET_SUNACT_LEVEL_CS: _sunact_level,
    msmod.RPC_GET_MATCH_DURATION: _match_duration,
    msmod.RPC_QUERY_MY_TEAM_INFO_CS: _team_info,
}

# 把 1-3 的 Set* 系列挂上（真正改数据 + 推同步，不再是假 ACK）
for _name, _spec in _SIMPLE_SETTERS.items():
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _make_setter(_spec, False)
for _name, _spec in _CONST_SETTERS.items():
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _make_setter(_spec, True)

# 编队内容（改的是 character_list 的子属性，不是根属性，单独处理）
for _name, _fn in (("SetCharacterList_CS", _set_character_list),
                   ("SetCharacterListName_CS", _set_character_list_name),
                   ("RequestSwitchStreet_CS", _switch_street),
                   ("RpcEnterStreet", _switch_street),
                   ("RequestLeaveStreet_CS", _leave_street),
                   ("RequestDoneStoryTaskParamInt_CS", _done_story_task),
                   ("RequestDoneStoryTaskParamStr_CS", _done_story_task),
                   ("RequestStreetActor_CS", _street_actor),
                   ("RequestFetchTask_CS", _task_fetch),
                   ("RequestDoneTask_CS", _task_done),
                   ("RequestCompleteTask_CS", _task_done),
                   ("RequestCompleteUnitlTask_CS", _task_done),
                   ("RequestDoneSomeTypeTask_CS", _task_done_type),
                   ("RpcLeaveSpace", _leave_space_rpc),
                   ("Street_Actor_InteractID_Update_CS", _actor_interact),
                   ("Street_Redpoint_Update_CS", _street_redpoint),
                   ("updateStreetTraceTask_CS", _street_trace_task),
                   ("RequestEnterStreetSurface_CS", _enter_surface),
                   ("RequestStreetTreasure_CS", _street_treasure_rpc),
                   ("Street_Mainline_Levelup_CS", _mainline_levelup),
                   ("finishStreetStoryline_CS", _finish_storyline),
                   ("RpcGetEdenChatHistory", _eden_chat_history),
                   ("RequestAddPrestigeExp_CS", _add_prestige_exp),
                   ("RequestGetPrestigeAward_CS", _get_prestige_award),
                   ("RequestGetPrestigeTaskAward_CS", _get_prestige_task_award),
                   ("RequestDispatchChar_CS", _dispatch_char),
                   ("RequestFinishDispatch_CS", _finish_dispatch),
                   ("RequestCancelDispatchChar_CS", _cancel_dispatch),
                   ("RequestUpgradeDungeonMainPower_CS", _ack_only),
                   ("RequestGetStreetLevelAchieveAward_CS", _ack_only),
                   ("Street_CD_Update_CS", _street_cd_update),
                   ("RequestRefreshTitleDailyTask_CS", _ack_only),
                   ("RequestDoneTitleTask_CS", _ack_only),
                   ("RollbackSpaceTask_CS", _ack_only),
                   ("Cmd_OfflineSpace_Finish_CS", _dungeon_finish),
                   ("Cmd_Dungeon_FinishInteract_CS", _ack_only),
                   ("Cmd_Infinite_FinishInteract_CS", _ack_only),
                   ("Cmd_Avatar_StartGame_CS", _avatar_start_game),
                   ("Storyline_FinishGame", _storyline_finish_game),
                   ("Cmd_Avatar_EnterNext_CS", _avatar_enter_next),
                   ("Cmd_Avatar_FinishGame_CS", _dungeon_finish),
                   ("BattleTeach_Activate_CS", _battle_teach_activate),
                   ("RecordItemObtain_CS", _ack_only)):
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _fn


# ---------------------------------------------------------------------------
# 单向消息处理器（serverMsg / CallServer，rid = 0）
# ---------------------------------------------------------------------------
# 这些不回应答，只改服务端数据。


# 位置上报很频繁（每次移动都发），不能每次都写盘。
# 同一个 uid 每 5 秒最多落盘一次；正常退出/切场景时 force 写。
_LAST_SAVE = {}
_SAVE_INTERVAL = 5.0


def _throttled_save(st, uid, force=False):
    """节流落盘：同一账号 5 秒内最多写一次。"""
    key = uid or ""
    now = time.time()
    if not force and (now - _LAST_SAVE.get(key, 0)) < _SAVE_INTERVAL:
        return
    _LAST_SAVE[key] = now
    try:
        st.save()
    except Exception as e:
        print("[save] 落盘失败 %s: %s" % (key, e), flush=True)


def _update_street_pos(params, uid=None):
    """RequestUpdateStreetPos_CS {street_id, pos_info} —— 客户端移动时上报位置。

    逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:130-135：
      function AvatarStreetComp:updatePlayerPos_CS(pos_info)
        self:serverMsg("RequestUpdateStreetPos_CS", {self.street_id, pos_info})
      end
    调用点：streetplayeractormovesystem.lua:120 / streetmovecomponent.lua:309 /
            streetactorbase.lua:381

    pos_info 形如 {x=, y=, z=, dir=}（streetsystemmgrbase.lua:561-566 读它恢复位置）。
    服务端存下来，下次登录通过 street_pos_info 下发即可回到原位。
    """
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return
    street_id = vals[0]
    pos = vals[1]
    if not isinstance(pos, dict):
        return
    st = _state(uid)
    if int(st.props.get("street_id") or 0) != int(street_id or 0):
        return
    saved = {
        "x": float(pos.get("x") or 0),
        "y": float(pos.get("y") or 0),
        "z": float(pos.get("z") or 0),
        "dir": float(pos.get("dir") or 0),
    }
    st.props["_oo_street_pos"] = saved
    _throttled_save(st, uid, force=True)


_ONE_WAY_HANDLERS = {
    294469585: _update_street_pos,   # RequestUpdateStreetPos_CS
    1277075552: _enter_space,        # RpcEnterSpace
    996547331: _scene_loaded,        # RpcOnSceneLoaded
    1588890197: _enter_eden,         # RpcEnterEden
    1369842456: _enter_eden,         # RpcBackToEden
    1920167376: _client_move,        # RpcOnClientMove（AOI）
    1517096639: _change_micro_state, # RpcChangeMicroState
    1675033651: _clear_treasure_actor,  # RequestClearStreetTreasureActor_CS
    2137482665: _leave_surface,         # RequestLeaveStreetSurface_CS
    1282426820: _touch_treasure,        # RequestTouchTreasure_CS
    2000707256: _play_act_emoji,        # RpcPlayActEmoji（AOI）
    2099437863: _noop_one_way,          # RpcEnterTargetSeq
    2031265256: _noop_one_way,          # Cmd_Avatar_StatEnterBattleFialed_CS
    790742624: _noop_one_way,           # Cmd_LoadingProcessSync_CS
    # --- Soul / 战斗（Phase 3 才真正跑起来，先把面补全） ---
    1768525703: _soul_heartbeat,        # soulHeartbeat_CS
    1136123026: _reliable_property_sync, # Cmd_Property_ReliableSync_CS
    1640580279: _space_frame_message,   # spaceFrameMessage
    577431259: _noop_one_way,           # spaceRpcMessage
    780255370: _noop_one_way,           # defineRpcMessageToLogic
    670252998: _noop_one_way,           # sendDungeonMsgToServer
    1740981687: _noop_one_way,          # Space_CallSpaceMethod_CS
    1909403499: _noop_one_way,          # continueSendMsg_CS
    1062918872: _noop_one_way,          # RpcSoulAckReliableMsg
    531534451: _noop_one_way,           # Cmd_Soul_WatchOtherPlayer_CS
    1936154067: _noop_one_way,          # sendDDZMessage
    739755462: _noop_one_way,           # sendMahjongMessage
    649611053: _noop_one_way,           # earlyLeaveDDZ
    404472060: _noop_one_way,           # earlyLeaveMahjong
}


def handle(index, params, rid, uid=None):
    """处理一条实体 RPC，返回**帧列表**（可能为空）。

    uid：这条连接对应的账号 uid（多账号时由服务端从 gate 连接上带过来）。

    返回列表是因为有的 RPC 除了应答帧，还要推额外的帧（比如 SetSignature_CS
    改完签名要再推一条 propertySync2Client）。

    空列表表示这条不用应答：
      - index 为 None；
      - rid <= 0 的单向消息（serverMsg / CallServer），回了客户端反而会报
        "entityMessageResponse got rid 0"（gateclientrpchandler.lua:138）。
    """
    if index is None:
        return []
    # ★ 单向消息（serverMsg / CallServer，rid = 0）以前在这里直接 return []，
    #   处理器根本没被调用 —— 但服务端**必须**处理它们（位置上报、红点、
    #   清理等全是单向的），只是不能回应答帧。
    #   所以单向消息走 _ONE_WAY_HANDLERS，只改数据、不回包。
    if rid <= 0:
        fn1 = _ONE_WAY_HANDLERS.get(index)
        if fn1 is not None:
            try:
                out = fn1(params, uid)
            except Exception as e:
                print("[rpc] 单向处理失败 index=%s: %s" % (index, e), flush=True)
                out = None
            # 单向处理器可以返回「服务端主动下发的 S2C 帧」（比如 RpcOnEnterSpace）
            if isinstance(out, list):
                return out
        return []
    fn = _HANDLERS.get(index)
    if fn is not None:
        out = fn(rid, params, uid)
        if out is None:
            return []
        return out if isinstance(out, list) else [out]
    # 通用成功应答：所有 _SC 处理函数都只看 retstatus.status。
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK))]


def decode_params(params):
    """把 msgpack 参数解成 Python 值，解不出来就返回 None。"""
    if not params:
        return None
    try:
        val, _ = msmod.mp_decode(params)
        return val
    except Exception:
        return None
