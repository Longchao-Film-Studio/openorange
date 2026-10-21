#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色数据 RPC（P0-E2）。

UpgradeCharacter_CS：加 CharLevel + 推 characterBag 子属性同步。
客户端 avatarcharcomp.lua:1015-1029 的 UpgradeCharacter_SC 在
addLevel > 0 时 post ECharLevelUp（升级特效）。

characterBag 结构（initDict 实测）：
  {__tp__:"CharacterPersistentDict", __id__:1326, 9001:{__id__:1341, ...}}
键在 _conv_int_keys 之后是整数 9001；但 proto 的 name 字段是字符串，
所以这里传 str(key)，客户端 _changeSubProperty 里会 tonumber 回来。
"""
from .. import ms as msmod
from ..rpc_common import decode_params, state, sync_frame
from ..rpc_index import ACK_OK
# ★ 不在这里 import story：story/__init__.py 要 import property.ids，
#   模块级反向 import 会形成 property <-> story 双向依赖（审计 P1-1）。
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
    # 延迟导入 story（避免 property <-> story 双向依赖，见文件头注释）
    from ..story import events as _events
    from ..story.rpc import _finish_story_tasks
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    char_id = vals[0]
    st = state(uid)
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
    # ★ P0-D：角色等级变化发 CharData 事件（覆盖 1 条剧情任务）
    res = _events.apply_event(
        st, _events.ev_char_data(level=int(char["CharLevel"]),
                                 char_id=key))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        frames.extend(_finish_story_tasks(st, nxt))
    st.save()
    frames.append(sync_frame(
        "", bag_id, msmod.OP_CHANGE, str(key), char))
    return frames
