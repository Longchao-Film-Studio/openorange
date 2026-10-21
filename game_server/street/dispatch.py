#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""街景派遣（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:277-300：
  RequestDispatchChar_CS {char_id, dip_id}    派角色去打工
  RequestFinishDispatch_CS {dip_id}           完成（领收益）
  RequestCancelDispatchChar_CS {dip_id}       取消
  getDispacthData(dip_id) = self.street_dispatch[dip_id]

street_dispatch 是 StreetDispatchDict（Dict<int, StreetDispatchInfo>），
客户端靠 entryAdded/entryDeleted/changed 三个回调刷新 UI，所以新增/删除/修改
必须推对应 optype：OP_ADD / OP_DEL / OP_CHANGE。
"""
import time

from .. import ms as msmod
from ..rpc_common import SYNC_SELF, decode_params, state, sync_frame
from ..rpc_index import ACK_OK
_DISPATCH_DEFAULT_DURATION = 3600

def _dispatch_dict(st):
    d = st.props.get("street_dispatch")
    if not isinstance(d, dict):
        return None
    return d

def _dispatch_char(rid, params, uid=None):
    """RequestDispatchChar_CS {char_id, dip_id}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return frames
    try:
        char_id = int(vals[0] or 0)
        dip_id = int(vals[1] or 0)
    except Exception:
        return frames
    st = state(uid)
    d = _dispatch_dict(st)
    if d is None:
        return frames
    now = int(time.time())
    entry = {
        "__tp__": "StreetDispatchInfo",
        "__id__": st.next_property_id(),
        "street_id": int(st.props.get("street_id") or 0),
        "dispatch_id": dip_id,
        "char_id": char_id,
        "beg_time": now,
        "end_time": now + _DISPATCH_DEFAULT_DURATION,
    }
    d[dip_id] = entry
    st.set("street_dispatch", d)
    st.save()
    print("[street] 派遣 char=%s dip=%s" % (char_id, dip_id), flush=True)
    frames.append(sync_frame(
        SYNC_SELF, d.get("__id__") or 0, msmod.OP_ADD, str(dip_id), entry))
    return frames

def _finish_dispatch(rid, params, uid=None):
    """RequestFinishDispatch_CS {dip_id} —— 完成派遣，把 end_time 提前到 now。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        dip_id = int(vals[0] or 0)
    except Exception:
        return frames
    st = state(uid)
    d = _dispatch_dict(st)
    if d is None or not isinstance(d.get(dip_id), dict):
        return frames
    entry = d[dip_id]
    entry["end_time"] = int(time.time())
    st.set("street_dispatch", d)
    st.save()
    print("[street] 完成派遣 dip=%s" % dip_id, flush=True)
    frames.append(sync_frame(
        SYNC_SELF, d.get("__id__") or 0, msmod.OP_CHANGE, str(dip_id), entry))
    return frames

def _cancel_dispatch(rid, params, uid=None):
    """RequestCancelDispatchChar_CS {dip_id} —— 取消派遣。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        dip_id = int(vals[0] or 0)
    except Exception:
        return frames
    st = state(uid)
    d = _dispatch_dict(st)
    if d is None or dip_id not in d:
        return frames
    del d[dip_id]
    st.set("street_dispatch", d)
    st.save()
    print("[street] 取消派遣 dip=%s" % dip_id, flush=True)
    frames.append(sync_frame(
        SYNC_SELF, d.get("__id__") or 0, msmod.OP_DEL, str(dip_id), None))
    return frames
