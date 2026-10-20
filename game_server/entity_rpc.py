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
        # 发奖励
        for item_id, _cnt in story.story_award(st, tid):
            bag = st.props.get("item_bag")
            if not isinstance(bag, dict):
                break
            for key, grid in bag.items():
                if isinstance(grid, dict) and grid.get("itemId") == item_id:
                    frames.append(msmod.build_property_sync(
                        "", bag.get("__id__") or 0, msmod.OP_CHANGE, str(key), grid))
                    break
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


def _touch_treasure(params, uid=None):
    """RequestTouchTreasure_CS {treasure_id} / RequestStreetTreasure_CS {treasure_id}。

    标记该宝箱已开：StreetInfo.treasure_box_info[treasure_id] = 1。
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
    tb[tid] = 1
    st.set("street_info", si)
    st.save()
    print("[street] 开宝箱 %s（街区 %s）" % (tid, street_id), flush=True)
    return [msmod.build_property_sync(
        "", si.get("__id__") or 0, msmod.OP_CHANGE, str(street_id), entry)]


def _street_treasure_rpc(rid, params, uid=None):
    """RequestStreetTreasure_CS {treasure_id} —— 带回调版本。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    out = _touch_treasure(params, uid)
    if out:
        frames.extend(out)
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
                   ("RequestStreetTreasure_CS", _street_treasure_rpc)):
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
