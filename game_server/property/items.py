#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""背包与货币相关 RPC（P0-E2）。

  UseItem_CS          使用道具：扣数量 + 应用 ItemData 声明的效果
  SubmitItem_CS       提交道具（发 SubmitItem 事件，覆盖 15 条剧情任务）
  RecordItemObtain_CS 记录获得道具（发 GetItem 事件）

★ 同步方式：item_bag 是 CustomDict，改某个格子的数量属于「改子属性」：
  propertyId = item_bag 的 __id__，name = 格子键（必须是字符串，proto 是 bytes），
  value = 整个格子。客户端 _changeSubProperty 里比对 __id__，命中就原地 rochange。
"""
from .. import gamedata
from .. import ms as msmod
from ..rpc_common import respond_after_sync, SYNC_SELF, decode_params, state, sync_frame
from ..rpc_index import ACK_OK
# ★ 不在这里 import story：story/__init__.py 要 import property.ids，
#   模块级反向 import 会形成 property <-> story 双向依赖（审计 P1-1）。
#   真正用到的地方做函数内延迟导入。
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
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, syncs)
    item_id = vals[0]
    try:
        count = int(vals[1])
    except Exception:
        return respond_after_sync(rid, syncs)
    if count <= 0:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    bag = st.get("item_bag")
    if not isinstance(bag, dict):
        return respond_after_sync(rid, syncs)
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
        return respond_after_sync(rid, syncs)

    old_num = grid.get("num") or 0
    if old_num < count:
        return respond_after_sync(rid, syncs)

    # ---- 1) 扣数量 ----
    grid["num"] = old_num - count
    st.set("item_bag", bag)

    # ---- 2) 应用道具效果 ----
    # 规则全在 ItemData：useType + useParam。
    # 例：时砂之盒(110) = {useType:"avatarAttr", useParam:{TowerRewardKeys:60}}
    changed_props = _apply_item_effect(st, item_id, count)

    st.save()

    # ---- 3) 推同步 ----
    syncs.append(sync_frame(
        # ★ name 必须是字符串（ms.pb_bytes_field 里对 int 会 len() 抛 TypeError）
        #   审计 P0-5：旧存档的整数键会让这条 RPC 无应答 -> 客户端弱网转圈。
        SYNC_SELF, bag_id, msmod.OP_CHANGE, str(grid_key), grid))
    for prop, value in changed_props:
        syncs.append(sync_frame(
            SYNC_SELF, 0, msmod.OP_CHANGE, prop, value))
    return respond_after_sync(rid, syncs)

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
        from .. import gamedata
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
                from .. import stamina as stamina_mod
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

# 索引 -> 处理函数。没列进来的走通用 ACK。
def _submit_item(rid, params, uid=None):
    """SubmitItem_CS —— 提交道具，发 SubmitItem 事件（覆盖 15 条剧情任务）。

    实测 TaskEventData 里 SubmitItem 的 EventParam 是空的，
    所以只要发了这个 RPC 就能命中 —— 客户端只在真的提交时才发。
    """
    # 延迟导入 story（避免 property <-> story 双向依赖，见文件头注释）
    from ..story import events as _events
    from ..story.rpc import _finish_story_tasks
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    submit_id = None
    item_id, count = 0, 1
    item_ids = []
    if isinstance(vals, list) and vals:
        # 客户端签名：SubmitItem_CS(submit_id, itemDict)
        #   avatarbagcomp.lua:930 / interactfunctionfactory.lua:810
        first = vals[0]
        if isinstance(first, str):
            submit_id = first
        elif isinstance(first, int) and not isinstance(first, bool):
            # 兼容：有的调用点直接把 itemId 当第一个参数
            item_id = first
        # 第二个参数是 itemDict = {itemId: count}
        if len(vals) > 1 and isinstance(vals[1], dict):
            idict = vals[1]
            for k, v in idict.items():
                try:
                    iid = int(k)
                except Exception:
                    continue
                item_ids.append(iid)
                if not item_id:
                    item_id = iid
                    try:
                        count = int(v)
                    except Exception:
                        count = 1
        # 从 ItemSubmitData 反查这个 submit_id 要求的道具
        if submit_id:
            row = gamedata.table("ItemSubmitData") or {}
            info = row.get(submit_id) or {}
            need = info.get("itemId")
            if isinstance(need, dict):
                for k in need.keys():
                    if str(k).startswith("__") or k == "KeyList":
                        continue
                    try:
                        iid = int(k)
                    except Exception:
                        continue
                    if iid not in item_ids:
                        item_ids.append(iid)
                    if not item_id:
                        item_id = iid
    st = state(uid)
    res = _events.apply_event(
        st, _events.ev_submit_item(item_id, count, submit_id, item_ids))
    from ..story.tasks import drain_event_task_sync  # 延迟导入（避免循环）
    frames.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        frames.extend(_finish_story_tasks(st, nxt, source="event"))
    print("[story] 提交道具 submit_id=%s item=%s x%s 需求=%s"
          % (submit_id, item_id, count, item_ids), flush=True)
    st.save()
    return frames

def _record_item_obtain(rid, params, uid=None):
    """RecordItemObtain_CS —— 记录获得道具，发 GetItem 事件（覆盖 4 条剧情任务）。

    TaskEventData 里 GetItem 有两种 KeyList：
      ["itemId"]                       -> 只要 itemId
      ["itemType", "level", "grade"]   -> 道具类型/等级/品质
    客户端上报的参数里可能带这些，能取到就一起发。
    """
    # 延迟导入 story（避免 property <-> story 双向依赖，见文件头注释）
    from ..story import events as _events
    from ..story.rpc import _finish_story_tasks
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    item_id, count = 0, 1
    item_type = level = grade = None
    if isinstance(vals, list) and vals:
        first = vals[0]
        if isinstance(first, dict):
            try:
                item_id = int(first.get("itemId") or 0)
                count = int(first.get("count") or first.get("num") or 1)
            except Exception:
                pass
            for name, box in (("itemType", "item_type"), ("level", "level"),
                              ("grade", "grade")):
                v = first.get(name)
                if isinstance(v, int):
                    if box == "item_type":
                        item_type = v
                    elif box == "level":
                        level = v
                    else:
                        grade = v
        else:
            try:
                item_id = int(first or 0)
            except Exception:
                item_id = 0
            if len(vals) > 1:
                try:
                    count = int(vals[1] or 1)
                except Exception:
                    count = 1
    st = state(uid)
    res = _events.apply_event(
        st, _events.ev_get_item(item_id, count, item_type, level, grade))
    from ..story.tasks import drain_event_task_sync  # 延迟导入（避免循环）
    frames.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        frames.extend(_finish_story_tasks(st, nxt))
    print("[story] 获得道具 %s x%s" % (item_id, count), flush=True)
    st.save()
    return frames
