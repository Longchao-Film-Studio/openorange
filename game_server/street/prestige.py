#!/usr/bin/env python3
"""街景声望（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:
  getPrestigePrize_CS(streetId, level) -> RequestGetPrestigeAward_CS {streetId, level}  :137-139
  addPrestigeExp_CS(exp)               -> RequestAddPrestigeExp_CS {exp}               :141-143
  getPrestigeTaskAward_CS(streetId, prestigeTaskId)                                    :99-101

数据在 StreetInfo（generated/config.lua:6834）：
  prestige_street_id / prestige_level / prestige_exp /
  prestige_awarded（IntDictKeyInt）/ prestige_tasks（IntDictKeyInt）
升级阈值在 BDD StreetPrestigeData[streetId]（needExpNum / level / levelAward）。

★ 审计 P0-4：is_new 必须由 _prestige_info **显式返回**，不能塞进 entry 当临时字段 ——
  调用方是先 save 再 push，临时字段会被写进存档，重启后又对已存在的 entry 发 OP_ADD
  -> 客户端 id repeated! 覆盖缓存。
"""
from shared.log_tee import get_logger

from .. import gamedata
from ..property import ids as _ids
from ..property import tx as _tx
from ..rpc_common import decode_params, respond_after_sync, state
from ..write_guard import mutable
from .info import _street_info_of

_log = get_logger(__name__)
def _prestige_info(st, street_id=None):
    """取（或建）当前街区的 StreetInfo，并保证声望字段齐全。

    返回 (si, entry, street_id, is_new)。

    ★ 审计 P0-4：之前用 entry 上的 _oo_is_new 标记传「是否新建」，
      但调用方是**先 save 再 push**，标记被写进存档 -> 重启后又对已存在的
      entry 发 OP_ADD -> 客户端 id repeated! 覆盖缓存。
      现在改成返回值显式传递，不再往 entry 里塞任何临时字段。
    """
    if street_id is None:
        street_id = int(st.props.get("street_id") or 0)
    si = st.props.get("street_info")
    is_new = not (isinstance(si, dict) and isinstance(si.get(street_id), dict))
    si, entry = _street_info_of(st, street_id)
    if entry is None:
        return None, None, None, False
    # ★ 返回**纯副本**：调用方拿到后随便改，改完由 _prestige_push 的 Tx 落地。
    #   直接改 props 里的 entry 是裸写，守卫会抛并把连接打断（2026-11-17 事故）。
    entry = mutable(entry)
    if not isinstance(entry.get("prestige_awarded"), dict):
        entry["prestige_awarded"] = {"__id__": _ids.alloc_id(st.props)}
    if not isinstance(entry.get("prestige_tasks"), dict):
        entry["prestige_tasks"] = {"__id__": _ids.alloc_id(st.props)}
    if not entry.get("prestige_street_id"):
        entry["prestige_street_id"] = street_id
    return si, entry, street_id, is_new

def _prestige_push(st, street_id, entry):
    """推 StreetInfo 条目。新建的用 OP_ADD（否则客户端 oldVal nil 崩）。

    is_new 由 _prestige_info 显式返回（审计 P0-4：不再用 entry 上的临时字段）。
    """
    #新建和改已有条目都走同一个 Tx：OP_ADD / OP_CHANGE由「键在不在容器里」决定。
    #
    #以前 is_new分支单独手工发 OP_ADD并 strip_private剥嵌套 __tp__，是为了绕开
    #「整对象换值会把嵌套 __tp__也带过去」的问题；现在统一由 Tx处理，
    #两条路径合并成一条，少一个会分叉的地方。
    tx = _tx.Tx(st.props)
    tx.set(("street_info", street_id), entry)
    return tx.frames()

def _apply_prestige_exp(st, _si, entry, street_id, add, save=True):
    """给某街区加声望经验并处理升级，返回同步帧。

    宝箱奖励（StreetTreasureTaskData.AwardAttr.prestige_exp）和
    声望 RPC（RequestAddPrestigeExp_CS）共用这段逻辑。
    """
    frames = []
    if add <= 0 or entry is None:
        return frames
    entry["prestige_exp"] = int(entry.get("prestige_exp") or 0) + int(add)
    rows = gamedata.street_prestige(
        entry.get("prestige_street_id") or street_id)
    by_level = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("level"), int):
            by_level[row["level"]] = int(row.get("needExpNum") or 0)
    lv = int(entry.get("prestige_level") or 0)
    exp = int(entry["prestige_exp"])
    while True:
        need = by_level.get(lv + 1)
        if need is None or exp < need:
            break
        exp -= need
        lv += 1
    entry["prestige_level"] = lv
    entry["prestige_exp"] = exp
    _log.info(f"[street] 声望 +{add} -> level={lv} exp={exp}")
    # ★ 顺序不能反：entry 是**副本**，只有 _prestige_push 的 Tx 才把值写进 props。
    #   先 save 会把「还没落地」的旧值存进盘（静默丢数据）。
    frames.extend(_prestige_push(st, street_id, entry))
    if save:
        st.save()
    return frames

# 公开入口（供背包效果等其它领域调用；内部实现仍是 _apply_prestige_exp/_prestige_info）
apply_prestige_exp = _apply_prestige_exp
prestige_info_of = _prestige_info


def _add_prestige_exp(rid, params, uid=None):
    """RequestAddPrestigeExp_CS {exp} —— 加声望经验，够了就升级。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, [])
    try:
        add = int(vals[0] or 0)
    except Exception:
        return respond_after_sync(rid, [])
    if add <= 0:
        return respond_after_sync(rid, [])
    st = state(uid)
    si, entry, street_id = _prestige_info(st)[:3]
    if entry is None:
        return respond_after_sync(rid, [])
    syncs = _apply_prestige_exp(st, si, entry, street_id, add,
                               )
    return respond_after_sync(rid, syncs)

def _get_prestige_award(rid, params, uid=None):
    """RequestGetPrestigeAward_CS {streetId, level} —— 领声望等级奖励。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, [])
    try:
        street_id = int(vals[0] or 0)
        level = int(vals[1] or 0)
    except Exception:
        return respond_after_sync(rid, [])
    st = state(uid)
    _si, entry, sid = _prestige_info(st, street_id)[:3]
    if entry is None:
        return respond_after_sync(rid, [])
    entry["prestige_awarded"][level] = 1
    st.save()
    _log.info(f"[street] 领声望奖励 street={street_id} level={level}")
    syncs = _prestige_push(st, sid, entry)
    return respond_after_sync(rid, syncs)

def _get_prestige_task_award(rid, params, uid=None):
    """RequestGetPrestigeTaskAward_CS {streetId, prestigeTaskId} —— 领声望任务奖励。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, [])
    try:
        street_id = int(vals[0] or 0)
        task_id = int(vals[1] or 0)
    except Exception:
        return respond_after_sync(rid, [])
    st = state(uid)
    _si, entry, sid = _prestige_info(st, street_id)[:3]
    if entry is None:
        return respond_after_sync(rid, [])
    entry["prestige_tasks"][task_id] = 1
    st.save()
    _log.info(f"[street] 领声望任务奖励 street={street_id} task={task_id}")
    syncs = _prestige_push(st, sid, entry)
    return respond_after_sync(rid, syncs)
