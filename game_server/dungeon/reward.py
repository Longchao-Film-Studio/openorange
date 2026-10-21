#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本结算发奖（P0-E2 第 3 步）。

LevelData 里**没有任何奖励字段**，奖励完全来自客户端算好的 battle_items
（finishInfo[uid].battle_items = {itemId: 净获得数量}）。
发完还要发 GetItem 事件：客户端只对 6 个 firstObtainNotice 道具发
RecordItemObtain_CS，剧情任务要的 701/118 永远等不到 RPC。
"""
from .. import ms as msmod
from .. import story
from ..rpc_common import SYNC_SELF, sync_frame
from ..story.rpc import _emit_get_item, _finish_story_tasks
def _grant_battle_items(st, battle_items):
    """把 battle_items 里的道具发进背包，返回 (同步帧列表, reward_item_list)。"""
    frames = []
    reward_list = []
    granted = []
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
            frames.append(sync_frame(
                SYNC_SELF, bag.get("__id__") or 0,
                msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                str(gkey), grid))
        reward_list.append({"item_id": iid, "item_num": cnt})
        granted.append((iid, cnt))
        print("[dungeon] 结算发道具 %s x%s -> 现在 %s" % (iid, cnt, num),
              flush=True)
    # ★ P0-D：战斗里捡到的道具也发 GetItem 事件
    #   （审计 P0-2：客户端只对 6 个 firstObtainNotice 道具发 RPC，
    #    剧情任务要的道具服务端必须自己发事件）
    for _iid, _cnt in granted:
        _hits = _emit_get_item(st, _iid, _cnt)
        if _hits:
            frames.extend(_finish_story_tasks(st, _hits))
    return frames, reward_list
