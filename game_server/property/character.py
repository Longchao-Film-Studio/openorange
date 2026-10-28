#!/usr/bin/env python3
"""角色数据 RPC（P0-E2）。

UpgradeCharacter_CS：加 CharLevel + 推 characterBag 子属性同步。
客户端 avatarcharcomp.lua:1015-1029 的 UpgradeCharacter_SC 在
addLevel > 0 时 post ECharLevelUp（升级特效）。

characterBag 结构（initDict 实测）：
  {__tp__:"CharacterPersistentDict", __id__:1326, 9001:{__id__:1341, ...}}
键在 conv_int_keys 之后是整数 9001；但 proto 的 name 字段是字符串，
所以这里传 str(key)，客户端 _changeSubProperty 里会 tonumber 回来。
"""
from ..rpc_common import decode_params, respond_after_sync, state
from .tx import Tx as _Tx

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



def _int_or(value, default):
    """int(value) 失败就用 default（原实现是 try/except，语义一致）。"""
    try:
        return int(value)
    except Exception:
        return default


def _count_owned(item_bag, item_id):
    """背包里某 itemId 的总数量（跨所有格子累加，跳过 __ 元数据键）。"""
    owned = 0
    for gkey, grid in item_bag.items():
        if str(gkey).startswith("__") or not isinstance(grid, dict):
            continue
        if grid.get("itemId") == item_id:
            owned += int(grid.get("num") or 0)
    return owned


def _upgrade_find_key(bag, char_id):
    """在 characterBag 里定位 char_id 的键（int / str / 数字串三种形态都试）。"""
    if char_id in bag:
        return char_id
    if str(char_id) in bag:
        return str(char_id)
    if isinstance(char_id, str) and char_id.isdigit() and int(char_id) in bag:
        return int(char_id)
    return None


def _upgrade_locate_bag(bag, char_id):
    """在 characterBag 里定位角色条目。返回 (bag_id, key, char) 或 None。"""
    bag_id = bag.get("__id__") or 0
    key = _upgrade_find_key(bag, char_id)
    if key is None:
        return None
    char = bag.get(key)
    if not isinstance(char, dict):
        return None
    return bag_id, key, char


def _upgrade_settle_cost(gd, item_bag, send_items):
    """按表重算升级消耗。返回 ((total_exp, gold_cost, consume), None) 或 (None, errorcode)。

    ★ 不信任客户端上报的经验/金币数值，只信 sendItems 的「要消耗哪些道具」意图。
    """
    total_exp = 0
    consume = {}
    for k, v in (send_items or {}).items():
        try:
            iid = int(k)
            cnt = int(v)
        except (TypeError, ValueError):
            return None, 15002
        if cnt <= 0:
            continue
        if iid not in _UPGRADE_EXP_ITEMS:
            return None, 15002          # 非升级材料
        it = gd.item(iid) or {}
        exp_each = int((it.get("useParam") or {}).get("CharExp") or 0)
        if exp_each <= 0:
            return None, 15002
        if _count_owned(item_bag, iid) < cnt:
            return None, 15005          # Bag_InvaildNum
        consume[iid] = consume.get(iid, 0) + cnt
        total_exp += exp_each * cnt
    if total_exp <= 0:
        return None, 15005
    gold_ratio = int(gd.const("UpgradeGoldRatio") or 0) or 0.5
    gold_cost = int(total_exp * float(gold_ratio))
    if gold_cost > 0 and _count_owned(item_bag, _UPGRADE_COIN_ID) < gold_cost:
        return None, 15008              # 金币不足（客户端弹 140）
    return (total_exp, gold_cost, consume), None


def _upgrade_consume(st, consume, gold_cost, items_mod):
    """扣材料 + 金币（走 items.consume_item 同一入口）。返回 (frames, None) 或 (None, errorcode)。"""
    out = []
    for iid, cnt in consume.items():
        frames = items_mod.consume_item(st, iid, cnt)
        if frames is None:
            return None, 15005
        out.extend(frames)
    if gold_cost > 0:
        frames = items_mod.consume_item(st, _UPGRADE_COIN_ID, gold_cost)
        if frames is None:
            return None, 15008
        out.extend(frames)
    return out, None


def _upgrade_advance(gd, key, old_level, max_level, cur_exp, total_exp):
    """逐级推进（客户端 getLevelUpInfo 同款算法，溢出进 CharExp）。返回 (new_level, cur_exp)。"""
    ups = gd.char_upgrade(key) or []
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
    return new_level, cur_exp



def _upgrade_emit_story(st, key, new_level, events_mod):
    """角色等级变化 -> 剧情事件 + 任务推进。返回同步帧列表（无事件时为空）。"""
    out = []
    res = events_mod.apply_event(
        st, events_mod.ev_char_data(level=int(new_level), char_id=key))
    from ..story.tasks import drain_event_task_sync  # 延迟导入（避免循环）
    out.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        from ..story.rpc import finish_story_tasks
        out.extend(finish_story_tasks(st, nxt, source="event"))
    return out


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
    located = _upgrade_locate_bag(bag, char_id)
    if located is None:
        return _upgrade_failed(rid, syncs, errorcode=7009)  # Userdata_CharNotFound
    _bag_id, key, char = located

    max_level = _gd.char_max_level(key)
    old_level = _int_or(char.get("CharLevel") or 1, 1)
    cur_exp = _int_or(char.get("CharExp") or 0, 0)
    if old_level >= max_level:
        return _upgrade_failed(rid, syncs)   # 已满级

    # ---- 2) sendItems 合法性 + 经验/金币结算（全部按表重算，不信任客户端数值）----
    item_bag = st.get("item_bag")
    if not isinstance(item_bag, dict):
        return _upgrade_failed(rid, syncs, errorcode=15002)  # Bag_InvaildItemId
    settled, errcode = _upgrade_settle_cost(_gd, item_bag, send_items)
    if errcode is not None:
        return _upgrade_failed(rid, syncs, errorcode=errcode)
    total_exp, gold_cost, consume = settled

    # ---- 3) 扣材料 + 金币 ----
    item_syncs, errcode = _upgrade_consume(st, consume, gold_cost, _items_mod)
    if errcode is not None:
        return _upgrade_failed(rid, syncs, errorcode=errcode)
    syncs.extend(item_syncs)

    # ---- 4) 逐级升级 ----
    new_level, cur_exp = _upgrade_advance(_gd, key, old_level, max_level,
                                         cur_exp, total_exp)
    if new_level == old_level and cur_exp == _int_or(char.get("CharExp") or 0, 0):
        # 经验不足以推进任何进度（比如 0）——拒绝，避免白扣
        return _upgrade_failed(rid, syncs, errorcode=15005)
    if new_level >= max_level:
        cur_exp = 0     # 满级经验清零（客户端 getCharacterCurExp 满级返回 needExp）
    #新值写进副本再交给 Tx：Tx靠「新旧值是否相等」决定发不发帧，
    #提前改 char本身会让新旧相等 ->帧丢失；但副本必须保留 __id__（否则客户端认不出是同一个角色）。
    # char是 characterBag里的活引用，改副本不会影响它，Tx.set会把副本写回去。
    char_new = dict(char)
    char_new["CharLevel"] = new_level
    char_new["CharExp"] = cur_exp
    _tx = _Tx(st.props)
    _tx.set(("characterBag", key), char_new)
    syncs.extend(_upgrade_emit_story(st, key, new_level, _events))
    st.save()
    syncs.extend(_tx.frames())
    return respond_after_sync(rid, syncs)

