#!/usr/bin/env python3
"""副本结算发奖（P0-E2 第 3 步）。

LevelData 里**没有任何奖励字段**，奖励完全来自客户端算好的 battle_items
（finishInfo[uid].battle_items = {itemId: 净获得数量}）。
发完还要发 GetItem 事件：客户端只对 6 个 firstObtainNotice 道具发
RecordItemObtain_CS，剧情任务要的 701/118 永远等不到 RPC。
"""
from shared.log_tee import get_logger

from ..story.rpc import _emit_get_item, finish_story_tasks

_log = get_logger(__name__)


def _grant_battle_items(st, battle_items):
    """把 battle_items 里的道具发进背包，返回 (同步帧列表, reward_item_list)。

    ★ 2026-10-26 收敛：发放走 property.bag.grants（堆叠/maxNum/钥匙/ext/过期/autoUse
      全在那里），同步帧由 grants.sync_frames 统一生成 —— 结算/宝箱/管理接口不再各拼一份。
    """
    frames = []
    reward_list = []
    granted = []
    if not isinstance(battle_items, dict):
        return frames, reward_list

    items_map = {}
    for k, v in battle_items.items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        try:
            iid = int(k)
            cnt = int(v)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
        if cnt > 0:
            items_map[iid] = items_map.get(iid, 0) + cnt
    if not items_map:
        return frames, reward_list

    from ..property.bag import grants
    _tx, outcomes = grants.grant_many(st.props, items_map, source="dungeon")
    # 帧必须走 _tx.frames()：grant_many 的变更不止物品格（autoUse 的道具会直接改
    # 根属性，例如道具 2 改 exp），手工拼物品格的帧会把这些漏掉。
    frames.extend(_tx.frames())
    for oc in outcomes:
        if not oc.ok:
            _log.warning("[dungeon] 结算发道具失败 %s：%s", oc.item_id, oc.error)
            continue
        reward_list.append({"item_id": oc.item_id, "item_num": oc.count})
        granted.append((oc.item_id, oc.count))
        _log.info("[dungeon] 结算发道具 %s x%s", oc.item_id, oc.count)
    # ★ P0-D：战斗里捡到的道具也发 GetItem 事件
    #   （审计 P0-2：客户端只对 6 个 firstObtainNotice 道具发 RPC，
    #    剧情任务要的道具服务端必须自己发事件）
    for _iid, _cnt in granted:
        _hits = _emit_get_item(st, _iid, _cnt, frames=frames)
        if _hits:
            frames.extend(finish_story_tasks(st, _hits, source="event"))
    return frames, reward_list
