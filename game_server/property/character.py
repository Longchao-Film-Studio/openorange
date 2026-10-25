#!/usr/bin/env python3
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
from ..rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame

# ★ 不在这里 import story：story/__init__.py 要 import property.ids，
#   模块级反向 import 会形成 property <-> story 双向依赖（审计 P1-1）。
# 升级材料（客户端 roleupgradepanel.lua 的 ExpBookIDs）：
#   201 电影胶卷·原片 CharExp=200 / 202 粗剪 600 / 203 精剪 1800
#   每个道具的经验值在 ItemData.useParam.CharExp（不再硬编码）
_UPGRADE_EXP_ITEMS = (201, 202, 203)
# 金币消耗 = addedExp × UpgradeGoldRatio（CommonConstData=0.5），扣 itemId=103
_UPGRADE_COIN_ID = 103


def _upgrade_failed(rid, syncs, errorcode=0):
    """升级失败的应答（retstatus.status=False + errorcode）。"""
    return respond_after_sync(
        rid, syncs, ret={"status": False, "errmsg": "", "errorcode": errorcode})


def _upgrade_character(rid, params, uid=None):
    """1-1 角色升级 —— 按客户端语义真实现（协议审计 T1，2026-10-24）。

    客户端规则（ui/panel/roleupgradepanel.lua + avatarcharactercomp.lua 全量取证）：
      UpgradeCharacter(charId, level, items)
        -> serverRpc("UpgradeCharacter_CS", {charId, sendItems}, ...)
      sendItems = {itemId: count}，全部是升级材料（ItemData.useParam.CharExp）
      addedExp  = Σ CharExp × count
      金币消耗  = addedExp × UpgradeGoldRatio（CommonConstData，扣 103）
      等级推进  = 逐级消耗 CharUpgradeData[charId][level].costExp.CharExp
      溢出经验  = 保留进 CharExp（客户端 getLevelUpInfo 同款算法）
      CharLevel / CharExp 存 characterBag[charId]，getCharacterCustomData 读取

    ★ 以前：固定 +1、忽略 sendItems —— 升级免费（经济漏洞，审计 T1 实锤）。
      现在：服务端按 BDD 表独立结算，sendItems 不足/金币不足/已达满级都拒绝。
      经验值/金币数不信任客户端上报，只信 sendItems 的「要消耗哪些道具」意图，
      数量与价格全部按表重算。

    应答：status=False 时带 errorcode（客户端 CheckCharacterStatus 走失败分支）。
    """
    from .. import gamedata as _gd
    from ..story import events as _events
    from ..story.rpc import _finish_story_tasks
    from . import items as _items_mod
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return _upgrade_failed(rid, syncs)
    char_id = vals[0]
    send_items = vals[1] if isinstance(vals[1], dict) else {}
    st = state(uid)
    bag = st.get("characterBag")
    if not isinstance(bag, dict):
        return _upgrade_failed(rid, syncs)
    bag_id = bag.get("__id__") or 0
    key = None
    if char_id in bag:
        key = char_id
    elif str(char_id) in bag:
        key = str(char_id)
    elif isinstance(char_id, str) and char_id.isdigit() and int(char_id) in bag:
        key = int(char_id)
    if key is None:
        return _upgrade_failed(rid, syncs, errorcode=7009)  # Userdata_CharNotFound
    char = bag.get(key)
    if not isinstance(char, dict):
        return _upgrade_failed(rid, syncs, errorcode=7009)

    max_level = _gd.char_max_level(key)
    try:
        old_level = int(char.get("CharLevel") or 1)
    except Exception:
        old_level = 1
    try:
        cur_exp = int(char.get("CharExp") or 0)
    except Exception:
        cur_exp = 0
    if old_level >= max_level:
        return _upgrade_failed(rid, syncs)   # 已满级

    # ---- 1) sendItems 合法性 + 经验/金币结算（全部按表重算，不信任客户端数值）----
    item_bag = st.get("item_bag")
    if not isinstance(item_bag, dict):
        return _upgrade_failed(rid, syncs, errorcode=15002)  # Bag_InvaildItemId
    total_exp = 0
    consume = {}          # {itemId: count}
    for k, v in (send_items or {}).items():
        try:
            iid = int(k)
            cnt = int(v)
        except (TypeError, ValueError):
            return _upgrade_failed(rid, syncs, errorcode=15002)
        if cnt <= 0:
            continue
        if iid not in _UPGRADE_EXP_ITEMS:
            return _upgrade_failed(rid, syncs, errorcode=15002)  # 非升级材料
        it = _gd.item(iid) or {}
        exp_each = int((it.get("useParam") or {}).get("CharExp") or 0)
        if exp_each <= 0:
            return _upgrade_failed(rid, syncs, errorcode=15002)
        # 背包里真有这么多吗（按 itemId 累加所有格子）
        owned = 0
        for gkey, grid in item_bag.items():
            if str(gkey).startswith("__") or not isinstance(grid, dict):
                continue
            if grid.get("itemId") == iid:
                owned += int(grid.get("num") or 0)
        if owned < cnt:
            return _upgrade_failed(rid, syncs, errorcode=15005)  # Bag_InvaildNum
        consume[iid] = consume.get(iid, 0) + cnt
        total_exp += exp_each * cnt
    if total_exp <= 0:
        return _upgrade_failed(rid, syncs, errorcode=15005)

    gold_ratio = int(_gd.const("UpgradeGoldRatio") or 0) or 0.5
    gold_cost = int(total_exp * float(gold_ratio))
    if gold_cost > 0:
        coin_owned = 0
        for gkey, grid in item_bag.items():
            if str(gkey).startswith("__") or not isinstance(grid, dict):
                continue
            if grid.get("itemId") == _UPGRADE_COIN_ID:
                coin_owned += int(grid.get("num") or 0)
        if coin_owned < gold_cost:
            return _upgrade_failed(rid, syncs, errorcode=15008)  # Bag_LevelNotEnough(金币不足，客户端弹 140)

    # ---- 2) 扣材料 + 金币（复用 items 的扣格逻辑，走同一入口）----
    item_syncs = []
    for iid, cnt in consume.items():
        frames = _items_mod.consume_item(st, iid, cnt)
        if frames is None:
            return _upgrade_failed(rid, syncs, errorcode=15005)
        item_syncs.extend(frames)
    if gold_cost > 0:
        frames = _items_mod.consume_item(st, _UPGRADE_COIN_ID, gold_cost)
        if frames is None:
            return _upgrade_failed(rid, syncs, errorcode=15008)
        item_syncs.extend(frames)
    syncs.extend(item_syncs)

    # ---- 3) 逐级升级（客户端 getLevelUpInfo 同款算法，溢出进 CharExp）----
    ups = _gd.char_upgrade(key) or []
    add_exp = total_exp
    new_level = old_level
    while new_level < max_level:
        row = ups[new_level - 1] if 0 < new_level <= len(ups) else None
        need = int((row or {}).get("costExp", {}).get("CharExp", 0)) if isinstance(row, dict) else 0
        if need <= 0:
            break
        if add_exp < need:
            cur_exp += add_exp
            add_exp = 0
            break
        add_exp -= need
        new_level += 1
        cur_exp = 0
    if new_level == old_level and cur_exp == int(char.get("CharExp") or 0):
        # 经验不足以推进任何进度（比如 0）——拒绝，避免白扣
        return _upgrade_failed(rid, syncs, errorcode=15005)
    if new_level >= max_level:
        cur_exp = 0     # 满级经验清零（客户端 getCharacterCurExp 满级返回 needExp）
    char["CharLevel"] = new_level
    char["CharExp"] = cur_exp
    st.set("characterBag", bag)

    # ---- 4) 剧情/同步 ----
    res = _events.apply_event(
        st, _events.ev_char_data(level=int(new_level), char_id=key))
    from ..story.tasks import drain_event_task_sync  # 延迟导入（避免循环）
    syncs.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        syncs.extend(_finish_story_tasks(st, nxt, source="event"))
    st.save()
    syncs.append(sync_frame(
        SYNC_SELF, bag_id, msmod.OP_CHANGE, str(key), char))
    return respond_after_sync(rid, syncs)
