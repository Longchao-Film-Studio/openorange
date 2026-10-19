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

from . import ms as msmod

# ---------------------------------------------------------------------------
# 阶段 1 三个组件的 serverRpc 清单（名字 -> genRpcIndex）
# ---------------------------------------------------------------------------
# 出处：entities/avatarcomponent/avatarcharactercomp.lua / avatarbagcomp.lua /
#       avataruserdatacomp.lua 里所有 serverRpc("...") 调用点。
RPC_INDEX = {
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
def _sunact_level(rid, params):
    """RpcGetSunActLevel_CS -> RpcGetSunActLevel_SC。

    avatarsunactcomp.lua:315-321：
      RpcGetSunActLevel_SC(ret, response) -> self.sun_daily_level_ids = response.levelIds
    没有周常数据就给空表，客户端 response.levelIds 拿到 {}，索引得到 nil，
    avatarsunactcomp.lua:226 的 if not self.sun_daily_level_ids 也放行。
    """
    return msmod.build_sunact_level_sc(rid)


def _match_duration(rid, params):
    """RpcGetMatchDuration -> 应答。

    avatarmatch.lua:73-79：
      self:serverRpc("RpcGetMatchDuration", {}, function(retstatus, response)
        if retstatus.status then self.predictMatchTime = response.data or {} end
      end)
    """
    return msmod.build_match_duration_sc(rid)


def _team_info(rid, params):
    """QueryMyTeamInfo_CS -> 应答（该玩家不在任何队伍里）。

    avatarteamcomp.lua:688-707：
      QueryMyTeamInfo_SC(cb, retstatus, response)：
        status 为假 -> self:ResetTeamInfo()，cb(false)
      CheckTeamStatus -> TeamErrorResult(nil, nil)：errcode=-1 查不到
      TeamErrorOrient，安静返回，不会弹错误提示。
    """
    return msmod.build_team_info_sc(rid)


def _upgrade_character(rid, params):
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
    st = _state()
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


def _use_item(rid, params):
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
    st = _state()
    bag = st.get("item_bag")
    if not isinstance(bag, dict):
        return frames
    bag_id = bag.get("__id__") or 0
    for key, grid in list(bag.items()):
        if key in ("__tp__", "__id__"):
            continue
        if not isinstance(grid, dict) or grid.get("itemId") != item_id:
            continue
        old_num = grid.get("num") or 0
        new_num = max(0, old_num - count)
        grid["num"] = new_num
        st.set("item_bag", bag)
        st.save()
        frames.append(msmod.build_property_sync(
            "", bag_id, msmod.OP_CHANGE, key, grid))
        break
    return frames


def _set_signature(rid, params):
    """1-3 改签名：真正改服务端状态，再推一条属性同步。

    依据 entities/avatarcomponent/avataruserdatacomp.lua:348-352：
      SetSignature_CS(signature, callback)
        -> serverRpc("SetSignature_CS", {signature}, ...)
    属性同步格式见 ms.build_property_sync（描述符逆向出来的）。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if isinstance(vals, list) and vals and isinstance(vals[0], str):
        st = _state()
        st.set("signature", vals[0])
        st.save()
        # ★ propertyId = 0 表示「改根属性」，客户端会走
        #   puppetpropertycomponent.lua:233-235 的 _changeRootProperty(k, v)。
        #   之前传的是递增序号 1，那是「改子属性」的语义（要 _id 命中
        #   __CustomPropertiesCache__ 里的对象），属于用错了分支。
        frames.append(msmod.build_property_sync(
            "", 0, msmod.OP_CHANGE, "signature", vals[0]))
    return frames


def _state():
    """服务端可变玩家状态（懒加载，首次用 initDict 初始化）。"""
    from . import player_state as pstate
    # 和 ms._live_init_dict 保持一致：状态里一律用整数键
    props = msmod._conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    return pstate.get_state(props)


# 索引 -> 处理函数。没列进来的走通用 ACK。
_HANDLERS = {
    msmod.RPC_INDEX_SET_SIGNATURE_CS: _set_signature,
    msmod.RPC_INDEX_USE_ITEM_CS: _use_item,
    msmod.RPC_INDEX_UPGRADE_CHARACTER_CS: _upgrade_character,
    msmod.RPC_GET_SUNACT_LEVEL_CS: _sunact_level,
    msmod.RPC_GET_MATCH_DURATION: _match_duration,
    msmod.RPC_QUERY_MY_TEAM_INFO_CS: _team_info,
}


def handle(index, params, rid):
    """处理一条实体 RPC，返回**帧列表**（可能为空）。

    返回列表是因为有的 RPC 除了应答帧，还要推额外的帧（比如 SetSignature_CS
    改完签名要再推一条 propertySync2Client）。

    空列表表示这条不用应答：
      - index 为 None；
      - rid <= 0 的单向消息（serverMsg / CallServer），回了客户端反而会报
        "entityMessageResponse got rid 0"（gateclientrpchandler.lua:138）。
    """
    if index is None:
        return []
    if rid <= 0:
        return []
    fn = _HANDLERS.get(index)
    if fn is not None:
        out = fn(rid, params)
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
