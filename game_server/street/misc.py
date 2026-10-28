#!/usr/bin/env python3
"""主线等级 / 街区剧情线 / CD 选择（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua：
  Street_Mainline_Levelup_CS(area_id) -> serverRpc({area_id})   :261-263
    读侧 getMainLineLevel(area_id) = self.mainline_level[area_id]  :265-268
  finishStreetStoryline_CS {eventId}                             :752-757
  Street_CD_Update_CS {street_id, street_cd_id}                  :149-154
    SC 回调 responseCb(status)
"""
from shared.log_tee import get_logger

from ..property.tx import Tx
from ..rpc_common import decode_params, respond_after_sync, state
from ..write_guard import write_guard
from .info import _street_tx_begin

_log = get_logger(__name__)
def _mainline_levelup(rid, params, uid=None):
    """Street_Mainline_Levelup_CS {area_id} —— 主线关卡等级 +1。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        area_id = int(vals[0])
    except Exception:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    ml = st.props.get("mainline_level")
    if not isinstance(ml, dict):
        return respond_after_sync(rid, syncs)
    cur = int(ml.get(area_id) or 0)
    tx = Tx(st.props)
    tx.set(("mainline_level", area_id), cur + 1)
    st.save()
    _log.info(f"[street] 主线 {area_id} 等级 {cur} -> {cur + 1}")
    syncs.extend(tx.frames())
    return respond_after_sync(rid, syncs)

def _finish_storyline(rid, params, uid=None):
    #属性写入口，在这里开守卫（街区剧情线）。
    with write_guard("_finish_storyline"):
        """finishStreetStoryline_CS {eventId} —— 记录完成的街区剧情线事件。"""
        vals = decode_params(params)
        if not (isinstance(vals, list) and vals):
            return respond_after_sync(rid, [])
        try:
            event_id = int(vals[0])
        except Exception:
            return respond_after_sync(rid, [])
        st = state(uid)
        done = st.props.get("_oo_storyline_done")
        if not isinstance(done, list):
            done = []
        if event_id not in done:
            done.append(event_id)
        st.props["_oo_storyline_done"] = done
        st.save()
        _log.info(f"[street] 街区剧情线完成 event={event_id}")
        return respond_after_sync(rid, [])

def _street_cd_update(rid, params, uid=None):
    """Street_CD_Update_CS {street_id, street_cd_id} —— 记录当前选的 CD 并回成功。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return respond_after_sync(rid, syncs)
    try:
        street_id = int(vals[0] or 0)
        cd_id = int(vals[1] or 0)
    except Exception:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    # ★ 用 Tx：新建的 street_info 条目自动 OP_ADD
    tx, si, entry, is_new = _street_tx_begin(st, street_id)
    if entry is not None:
        entry["cd_choose"] = cd_id
        st.set("street_info", si)
        if not is_new:
            tx.set(("street_info", street_id, "cd_choose"), cd_id)
        st.save()
        syncs.extend(tx.frames())
    _log.info(f"[street] CD 选择 street={street_id} cd={cd_id}")
    return respond_after_sync(rid, syncs)
