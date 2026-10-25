#!/usr/bin/env python3
"""空间/场景进出（P0-E2 第 2 步）。

逆向依据 entities/avatarcomponent/avatarspacecomp.lua：
  EnterSpace(spaceno, spaceid, pos, yaw, info)
    -> CallServer("RpcEnterSpace", spaceno, spaceid or "", info)   单向
  LeaveSpace() -> CallServer("RpcLeaveSpace")                       带回调
  OnSceneLoaded(spaceno) -> CallServer("RpcOnSceneLoaded", spaceno) 单向

服务端要做两件事：
  1. 改 space_p.spaceno / seqno / spaceid 并推属性同步（客户端 IsInHotSpace 读它）
  2. **主动下发** ServerOnlyMsg RpcOnEnterSpace(spaceno, spaceid, info)，
     客户端 RpcOnEnterSpace 里才会去 JumpToEdenStage / OnSceneLoaded 加载场景

space_p 结构（initDict）：
  {__tp__:SpaceProp, __id__:1200, spaces:{__id__:1201},
   spaceno:0, seqno:0, spaceid:"", loading:false, ltime:0, ld_spaceno:0}

SpaceConst.SPACE_EDEN = 1000（gamecommon/consts/spaceconst.lua:2）
"""
from .. import ms as msmod
from ..rpc_common import respond_after_sync, SYNC_SELF, decode_params, state, sync_frame

from shared.log_tee import get_logger
_log = get_logger(__name__)

SPACE_EDEN = 1000

# ServerOnlyMsg 的索引（genRpcIndex 现算，见 work/rpc_hashes.tsv）
RPC_ON_ENTER_SPACE = 1879036833      # RpcOnEnterSpace(int, string, table)
RPC_ON_TELEPORT_OUT = 354358832      # RpcOnTeleportOut(int)

def _space_prop(st):
    sp = st.props.get("space_p")
    if not isinstance(sp, dict):
        return None
    return sp

def _space_sync_frames(st, sp):
    """把 space_p 的 spaceno/seqno/spaceid 推给客户端。"""
    frames = []
    sp_id = sp.get("__id__") or 0
    for name in ("spaceno", "seqno", "spaceid", "loading", "ld_spaceno"):
        frames.append(sync_frame(
            SYNC_SELF, sp_id, msmod.OP_CHANGE, name, sp.get(name)))
    return frames

def _s2c_entity(method_name, index, params):
    """构造一条服务端 -> 客户端的 entityMessage。"""
    return msmod.build_entity_message(
        entity_id="", method_name=method_name, index=index,
        params=msmod.mp_arr(params) if isinstance(params, list) else params)

def _enter_space(params, uid=None):
    """RpcEnterSpace [spaceno, spaceid, info] —— 进/出空间。

    spaceno == 0 表示回到普通世界（离开空间）。
    """
    # params 可能是 msgpack 字节（来自客户端），也可能是已经解好的 list
    # （服务端内部调用，比如 _enter_eden 直接传 [SPACE_EDEN, ...]）。
    vals = params if isinstance(params, list) else decode_params(params)
    if not isinstance(vals, list):
        vals = []
    try:
        spaceno = int(vals[0]) if len(vals) > 0 and vals[0] is not None else 0
    except Exception:
        spaceno = 0
    spaceid = vals[1] if len(vals) > 1 and isinstance(vals[1], str) else ""
    info = vals[2] if len(vals) > 2 and isinstance(vals[2], dict) else {}
    st = state(uid)
    sp = _space_prop(st)
    if sp is None:
        return []
    old = int(sp.get("spaceno") or 0)
    sp["spaceno"] = spaceno
    sp["seqno"] = int(sp.get("seqno") or 0) + 1
    sp["spaceid"] = spaceid
    sp["loading"] = bool(spaceno)
    sp["ld_spaceno"] = 0
    st.set("space_p", sp)
    st.save()
    _log.info("[space] %s -> %s (spaceid=%s)" % (old, spaceno, spaceid))
    frames = _space_sync_frames(st, sp)
    if spaceno:
        frames.append(_s2c_entity(
            "RpcOnEnterSpace", RPC_ON_ENTER_SPACE,
            [spaceno, spaceid, info]))
    elif old:
        frames.append(_s2c_entity(
            "RpcOnTeleportOut", RPC_ON_TELEPORT_OUT, [old]))
    return frames

def _scene_loaded(params, uid=None):
    """RpcOnSceneLoaded [spaceno] —— 客户端场景加载完成。"""
    vals = decode_params(params)
    if not isinstance(vals, list) or not vals:
        return []
    try:
        spaceno = int(vals[0])
    except Exception:
        return []
    st = state(uid)
    sp = _space_prop(st)
    if sp is None:
        return []
    sp["loading"] = False
    sp["ld_spaceno"] = spaceno
    st.set("space_p", sp)
    st.save()
    _log.info("[space] 场景加载完成 %s" % spaceno)
    return _space_sync_frames(st, sp)

def _leave_space_rpc(rid, params, uid=None):
    """RpcLeaveSpace —— 离开空间（带回调，需要回应答）。"""
    syncs = []
    st = state(uid)
    sp = _space_prop(st)
    if sp is None:
        return respond_after_sync(rid, syncs)
    old = int(sp.get("spaceno") or 0)
    sp["spaceno"] = 0
    sp["seqno"] = int(sp.get("seqno") or 0) + 1
    sp["spaceid"] = ""
    sp["loading"] = False
    st.set("space_p", sp)
    st.save()
    _log.info("[space] 离开空间 %s" % old)
    syncs.extend(_space_sync_frames(st, sp))
    if old:
        syncs.append(_s2c_entity(
            "RpcOnTeleportOut", RPC_ON_TELEPORT_OUT, [old]))
    return respond_after_sync(rid, syncs)
