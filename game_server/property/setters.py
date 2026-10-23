#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Set*_CS 系列：改属性 + 推根/子属性同步（P0-E2 第 1 步）。

客户端 entities/avatarcomponent/avataruserdatacomp.lua 里每个 setter 都是

    SetXxx_CS(v, callback) -> serverRpc("SetXxx_CS", {v}, ...)

目标属性名对照 SystemConst.CommonUserDataType（common/systemconst.lua:1390）。

只处理「简单类型 + IntList」的属性：
  - 简单类型（int/bool/str）：直接改根属性；
  - IntList 等自定义类型：保留原来的 __tp__/__id__，用新值重建条目，
    客户端 _changeRootProperty 靠 __id__ 命中做原地 rochange。

查表类 RPC（QueryExpiredItems 等）和没有属性可改的（ForbidSelf）只回 ACK。

编队（character_list）是「改子属性」不是根属性，所以单独两个函数。
"""
from .. import ms as msmod
from ..rpc_common import (SYNC_SELF, decode_params, respond_after_sync, state,
                          sync_frame)

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
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, syncs)
    idx = vals[0]
    st = state(uid)
    cl = st.get("character_list")
    if not isinstance(cl, dict):
        return respond_after_sync(rid, syncs)
    team = cl.get(idx)
    if not isinstance(team, dict) and isinstance(idx, int):
        team = cl.get(str(idx))
    if not isinstance(team, dict):
        return respond_after_sync(rid, syncs)

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
    syncs.append(sync_frame(
        SYNC_SELF, cl.get("__id__") or 0, msmod.OP_CHANGE, str(idx), team))
    return respond_after_sync(rid, syncs)


def _set_character_list_name(rid, params, uid=None):
    """SetCharacterListName_CS {index, name}。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, syncs)
    idx, name = vals[0], vals[1]
    st = state(uid)
    cl = st.get("character_list")
    if not isinstance(cl, dict):
        return respond_after_sync(rid, syncs)
    team = cl.get(idx)
    if not isinstance(team, dict) and isinstance(idx, int):
        team = cl.get(str(idx))
    if not isinstance(team, dict):
        return respond_after_sync(rid, syncs)
    team["name"] = name if isinstance(name, str) else ""
    st.set("character_list", cl)
    st.save()
    syncs.append(sync_frame(
        SYNC_SELF, cl.get("__id__") or 0, msmod.OP_CHANGE, str(idx), team))
    return respond_after_sync(rid, syncs)


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
    st = state(uid)
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
    syncs = []
    prop_name, arg_idx = spec
    if arg_idx is None:
        new_value = spec[2] if len(spec) > 2 else None
    else:
        vals = decode_params(params)
        if not (isinstance(vals, list) and len(vals) > arg_idx):
            return respond_after_sync(rid, syncs)
        new_value = vals[arg_idx]
    applied = _apply_property(prop_name, new_value, uid)
    syncs.append(sync_frame(
        SYNC_SELF, 0, msmod.OP_CHANGE, prop_name, applied))
    return respond_after_sync(rid, syncs)


def _const_setter(rid, params, spec, uid=None):
    """无参数 setter：把属性置成常量 + 推同步。"""
    prop_name, const_value = spec
    syncs = []
    # ★ 必须把 uid 传下去。审计发现：之前漏传 -> _apply_property 内部
    #   state(None) 落到 DEFAULT_UID（OO00000001），
    #   真实用户的属性没改，却给他推了 value=1 的同步帧。
    #   canScore / hasScore 四个 RPC 全部中招。
    applied = _apply_property(prop_name, const_value, uid)
    syncs.append(sync_frame(
        SYNC_SELF, 0, msmod.OP_CHANGE, prop_name, applied))
    return respond_after_sync(rid, syncs)


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
    syncs = []
    vals = decode_params(params)
    if isinstance(vals, list) and vals and isinstance(vals[0], str):
        st = state(uid)
        st.set("signature", vals[0])
        st.save()
        # ★ propertyId = 0 表示「改根属性」，客户端会走
        #   puppetpropertycomponent.lua:233-235 的 _changeRootProperty(k, v)。
        #   之前传的是递增序号 1，那是「改子属性」的语义（要 _id 命中
        #   __CustomPropertiesCache__ 里的对象），属于用错了分支。
        syncs.append(sync_frame(
            SYNC_SELF, 0, msmod.OP_CHANGE, "signature", vals[0]))
    return respond_after_sync(rid, syncs)
