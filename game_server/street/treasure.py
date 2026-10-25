#!/usr/bin/env python3
"""街景宝箱（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:328-334, 353-363：
  RequestTouchTreasure_CS(treasure_id)  -> serverMsg（单向）
  RequestStreetTreasure_CS(treasure_id) -> serverRpc
  checkTreasureIsComplete 读 StreetInfo.treasure_box_info（IntDictKeyInt，0 < state 即已开）

★ 之前只把 treasure_box_info[tid] 标成 1，**奖励一个没发** ——
  玩家点开箱子只看到开箱动画，没道具也没声望，就是这个原因。
  StreetTreasureTaskData 44 条：AwardItemList（38 条）+ AwardAttr.prestige_exp（30 条）。
"""
from .. import gamedata, story
from .. import ms as msmod
from ..property import ids as _ids
from ..rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame
from .info import _street_tx_begin
from .prestige import _apply_prestige_exp, _prestige_info


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
                frames.append(sync_frame(
                    SYNC_SELF, bag.get("__id__") or 0,
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
        si2, entry2, s2, _n2 = _prestige_info(st, target)
        if entry2 is not None:
            frames.extend(_apply_prestige_exp(
                st, si2, entry2, s2, exp, save=False, is_new=_n2))
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
    st = state(uid)
    street_id = int(st.props.get("street_id") or 0)
    # ★ 用 Tx：新建的 street_info 条目自动 OP_ADD
    tx, si, entry, is_new = _street_tx_begin(st, street_id)
    if entry is None:
        return None
    tb = entry.get("treasure_box_info")
    if not isinstance(tb, dict):
        tb = {"__id__": _ids.alloc_id(st.props)}
        entry["treasure_box_info"] = tb
    already = tb.get(tid) == 1
    tb[tid] = 1
    st.set("street_info", si)
    print("[street] 开宝箱 %s（街区 %s）%s"
          % (tid, street_id, "重复开启，不再发奖" if already else ""), flush=True)
    if not is_new:
        tx.set(("street_info", street_id, "treasure_box_info"), tb)
    frames = list(tx.frames())
    if not already:
        frames.extend(_grant_treasure_reward(st, tid))
    st.save()
    return frames

def _street_treasure_rpc(rid, params, uid=None):
    """RequestStreetTreasure_CS {treasure_id} —— 带回调版本。"""
    syncs = _touch_treasure(params, uid) or []
    return respond_after_sync(rid, syncs)
