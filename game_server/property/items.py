#!/usr/bin/env python3
"""背包与货币相关 RPC（P0-E2）。

  UseItem_CS          使用道具：扣数量 + 应用 ItemData 声明的效果
  SubmitItem_CS       提交道具（发 SubmitItem 事件，覆盖 15 条剧情任务）
  RecordItemObtain_CS 记录获得道具（发 GetItem 事件）

★ 同步方式：item_bag 是 CustomDict，改某个格子的数量属于「改子属性」：
  propertyId = item_bag 的 __id__，name = 格子键（必须是字符串，proto 是 bytes），
  value = 整个格子。客户端 _changeSubProperty 里比对 __id__，命中就原地 rochange。
"""
from shared.log_tee import get_logger

from .. import gamedata
from .. import ms as msmod
from ..rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame

_log = get_logger(__name__)
# ★ 不在这里 import story：story/__init__.py 要 import property.ids，
#   模块级反向 import 会形成 property <-> story 双向依赖（审计 P1-1）。
#   真正用到的地方做函数内延迟导入。
# ErrorConst（gamecommon/consts/errorconst.lua，客户端 UseItem_SC 消费）
BAG_INVALID_ITEM_ID = 15002   # Bag_InvaildItemId
BAG_INVALID_NUM = 15005       # Bag_InvaildNum
BAG_SYSTEM_NOT_UNLOCKED = 15009  # Bag_SystemNotUnlocked（当前无拦截点，留作文档值）


def _use_failed(rid, syncs, errorcode=None):
    """UseItem 失败应答。errorcode 用客户端 ErrorConst 枚举值（审计 T2）。"""
    ret = {"status": False, "errmsg": ""}
    if errorcode is not None:
        ret["errorcode"] = int(errorcode)
    return respond_after_sync(rid, syncs, ret=ret)


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
        return _use_failed(rid, syncs, BAG_INVALID_ITEM_ID)
    item_id = vals[0]
    try:
        count = int(vals[1])
    except Exception:
        return _use_failed(rid, syncs, BAG_INVALID_NUM)
    if count <= 0:
        return _use_failed(rid, syncs, BAG_INVALID_NUM)
    st = state(uid)
    bag = st.get("item_bag")
    if not isinstance(bag, dict):
        return _use_failed(rid, syncs, BAG_INVALID_ITEM_ID)
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
        # ★ 审计 T2：背包没有此道具回 Bag_InvaildItemId（原来回成功——撒谎）
        return _use_failed(rid, syncs, BAG_INVALID_ITEM_ID)

    old_num = grid.get("num") or 0
    if old_num < count:
        return _use_failed(rid, syncs, BAG_INVALID_NUM)

    # unlockedSystem 门槛：只留痕不拦截（2026-10-24 复盘后去掉硬检查）
    #   客户端有 Bag_SystemNotUnlocked 分支，官服会拦；但私服没有经济动机拦
    #   「提前使用」，而判断依据是服务端 unlocked_system（任务驱动解锁链）——
    #   链路有偏差时会误判，把「任务没推进」放大成「道具坏了」（第二层故障）。
    #   将来要做「系统未开放」提示时，这里是现成挂点。
    it_cfg = gamedata.item(item_id) or {}
    need_sys = it_cfg.get("unlockedSystem")
    if need_sys:
        sysp = st.get("system_p")
        unlocked = (sysp or {}).get("unlocked_system") if isinstance(sysp, dict) else None
        try:
            is_unlocked = isinstance(unlocked, dict) and bool(unlocked.get(int(need_sys)))
        except (TypeError, ValueError):
            is_unlocked = True
        if not is_unlocked:
            print(f"[bag] 道具 {item_id} 需要 system {need_sys}（未解锁，放行留痕）", flush=True)

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
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
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
                _log.warning(f"[item] 体力加算失败: {e}")
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
    syncs = []
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
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
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
                for k in need:
                    if str(k).startswith("__") or k == "KeyList":
                        continue
                    try:
                        iid = int(k)
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        continue
                    if iid not in item_ids:
                        item_ids.append(iid)
                    if not item_id:
                        item_id = iid
    st = state(uid)
    res = _events.apply_event(
        st, _events.ev_submit_item(item_id, count, submit_id, item_ids))
    from ..story.tasks import drain_event_task_sync  # 延迟导入（避免循环）
    syncs.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        syncs.extend(_finish_story_tasks(st, nxt, source="event") or [])
    print(f"[story] 提交道具 submit_id={submit_id} item={item_id} x{count} 需求={item_ids}", flush=True)
    st.save()
    return respond_after_sync(rid, syncs)

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
    syncs = []
    vals = decode_params(params)
    item_id, count = 0, 1
    item_type = level = grade = None
    if isinstance(vals, list) and vals:
        first = vals[0]
        if isinstance(first, dict):
            try:
                item_id = int(first.get("itemId") or 0)
                count = int(first.get("count") or first.get("num") or 1)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
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
    syncs.extend(drain_event_task_sync(st, res))
    nxt = list(res.hits) + list(res.chain_hits)
    if nxt:
        syncs.extend(_finish_story_tasks(st, nxt) or [])
    _log.info(f"[story] 获得道具 {item_id} x{count}")
    st.save()
    return respond_after_sync(rid, syncs)


def consume_item(st, item_id, count):
    """从背包扣道具（跨格累计），返回 item_bag 同步帧列表；不够扣返回 None。

    ★ 为什么收进来（审计 T1）：升级/使用都要「按 itemId 扣 N 个」，以前只有
      「单格内扣」的 use_item 路径。材料可能散在多个格子，必须跨格累计。
      不改动格子的其它字段；扣到 0 保留格子（num=0，客户端显示逻辑自己处理）。
    """
    bag = st.get("item_bag")
    if not isinstance(bag, dict):
        return None
    bag_id = bag.get("__id__") or 0
    need = int(count)
    # 先确认总量够
    owned = 0
    for gkey, grid in bag.items():
        if str(gkey).startswith("__") or not isinstance(grid, dict):
            continue
        if grid.get("itemId") == item_id:
            owned += int(grid.get("num") or 0)
    if owned < need:
        return None
    frames = []
    for gkey, grid in list(bag.items()):
        if need <= 0:
            break
        if str(gkey).startswith("__") or not isinstance(grid, dict):
            continue
        if grid.get("itemId") != item_id:
            continue
        have = int(grid.get("num") or 0)
        take = min(have, need)
        if take <= 0:
            continue
        grid["num"] = have - take
        need -= take
        frames.append(sync_frame(
            SYNC_SELF, bag_id, msmod.OP_CHANGE, str(gkey), grid))
    st.set("item_bag", bag)
    return frames
