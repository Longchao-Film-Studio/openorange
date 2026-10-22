#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""街区切换 / 离开 / 位置上报（P0-E2 第 4 步）。

逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:107-128, 651-668：
  enterStreet_CS(id)        -> serverRpc("RpcEnterStreet", {id, streetParams})
  AvatarStreet_SwitchStreet -> serverRpc("RequestSwitchStreet_CS", {streetId, streetParams})
  leaveStreet_CS(id)        -> serverRpc("RequestLeaveStreet_CS", {id})
  updatePlayerPos_CS(pos)   -> serverMsg("RequestUpdateStreetPos_CS", {street_id, pos})

客户端拿到 status=true 后会自己 JumpToStreetStage 加载场景，**但小地图是按服务端
属性取的**（node_map_smallgroup.lua:30 读 getStreetID()），所以必须改 street_id 并推同步。

★ 审计修正：切街区时之前手写 OP_CHANGE 打**新建的** street_info 键，
  客户端 _changeSubProperty 里 oldVal 为 nil -> 整条同步被丢弃。
  表现：切街区后小地图/宝箱/交互状态全不同步。现在交给 Tx，新键自动用 OP_ADD。
"""
from .. import ms as msmod
from ..property import tx as _tx
from .. import gamedata
from .. import story
from ..rpc_common import SYNC_SELF, decode_params, state, sync_frame, throttled_save
from ..rpc_index import ACK_OK
from .info import _make_street_info_entry
def _switch_street(rid, params, uid=None):
    """RequestSwitchStreet_CS {streetId, streetParams} / RpcEnterStreet {id, streetParams}。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        new_id = int(vals[0])
    except Exception:
        return frames
    return _apply_street_change(frames, new_id, uid)

def _leave_street(rid, params, uid=None):
    """RequestLeaveStreet_CS {id} —— 离开街区，street_id 归 0。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    return _apply_street_change(frames, 0, uid)

def _refresh_trace_on_street_change(st, new_id, frames):
    """★ 切街区后重算追踪目标，并强制客户端刷新标点。

    实机反馈（2026-10-22）：「跟随任务指引到了下一个场景之后，
    任务还是在上一个场景标点，重过它还是一直在上一个的标点，压根无法进行」。

    客户端证据：
      ui/panel/streetv2panel/streettasktrackcomp.lua:14
        ui.addEvent(EUIEventDefine.EStreetTaskTracking, self.onTrackingTaskRefresh, self)
      —— 标点刷新**只**绑在 EStreetTaskTracking 事件上，
      而该事件由 on_street_tracing_task_id_changed 触发（属性 OP_CHANGE 时）。
      切街区**不会**触发它 -> 旧标点留在旧位置。

    服务端能做的两件事：
      1. 切街区时**重推一次** street_trace_taskid（哪怕值没变），
         客户端收到 OP_CHANGE 就会重算标点 —— 这一步就能修「标点不刷新」。
      2. 如果当前追踪的任务 `GuideSceneId` 不是新街区（在别的场景做不了），
         换成一个「在新街区、且正在做」的任务（GuideSceneId 覆盖 type1/2/4 达 92-100%）。
         找不到就保持原样（客户端会显示「前往 XX」提示）。
    """
    try:
        cur = int(st.props.get("street_trace_taskid") or 0)
    except Exception:
        cur = 0
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        bag = {}

    def _doing(t):
        it = bag.get(t)
        return isinstance(it, dict) and it.get("state") == story.TASK_STATE_DOING

    def _scene_of(t):
        cfg = gamedata.story_task(t) or {}
        gs = cfg.get("GuideSceneId")
        try:
            return int(gs) if gs is not None else None
        except Exception:
            return None

    nxt = cur
    if cur > 0 and new_id:
        cs = _scene_of(cur)
        if cs is not None and cs != int(new_id):
            cur_cfg = gamedata.story_task(cur) or {}
            cur_chapter = cur_cfg.get("ChapterId")
            cands = [int(t) for t in bag
                     if str(t).lstrip("-").isdigit()
                     and _doing(int(t))
                     and _scene_of(int(t)) == int(new_id)]

            def _rank(t):
                cfg = gamedata.story_task(t) or {}
                ch = cfg.get("ChapterId")
                # 同章节最优先（玩家本来就在这条线上）
                same = 0 if (cur_chapter and ch == cur_chapter) else 1
                # 其次主线章节（main_*）
                is_main = 0 if str(ch or "").startswith("main") else 1
                return (same, is_main, t)

            if cands:
                nxt = sorted(cands, key=_rank)[0]
                print("[street] 追踪目标 %s 不在街区 %s，切到 %s"
                      % (cur, new_id, nxt), flush=True)
    st.props["street_trace_taskid"] = nxt
    frames.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                             "street_trace_taskid", nxt))


def _apply_street_change(frames, new_id, uid):
    """改 street_id + 保证 street_info 有条目 + 推同步。

    ★ 2026-10-21 审计修正：这里之前手写 OP_CHANGE 打**新建的** street_info 键，
      客户端 _changeSubProperty 里 oldVal 为 nil -> oldVal:customTypeName()
      索引 nil -> 抛错 -> 整条同步被丢弃。
      表现：切街区后小地图/宝箱/交互状态全不同步。
      现在交给 Tx，新键自动用 OP_ADD。
    """
    st = state(uid)
    old = st.props.get("street_id")
    tx = _tx.Tx(st.props)
    tx.set(("street_id",), new_id)
    if new_id:
        si = st.props.get("street_info")
        if isinstance(si, dict) and not isinstance(si.get(new_id), dict):
            # 新街区：先构造（不插入），让 Tx 判定为 OP_ADD
            tx.set(("street_info", new_id), _make_street_info_entry(st))
    st.save()
    frames.extend(tx.frames())
    # ★ 切街区后刷新追踪标点（否则标点留在旧街区，玩家找不到下一步）
    _refresh_trace_on_street_change(st, new_id, frames)
    print("[street] %s -> %s" % (old, new_id), flush=True)
    return frames

def _update_street_pos(params, uid=None):
    """RequestUpdateStreetPos_CS {street_id, pos_info} —— 客户端移动时上报位置。

    逆向依据 entities/avatarcomponent/avatarstreetcomp.lua:130-135：
      function AvatarStreetComp:updatePlayerPos_CS(pos_info)
        self:serverMsg("RequestUpdateStreetPos_CS", {self.street_id, pos_info})
      end
    调用点：streetplayeractormovesystem.lua:120 / streetmovecomponent.lua:309 /
            streetactorbase.lua:381

    pos_info 形如 {x=, y=, z=, dir=}（streetsystemmgrbase.lua:561-566 读它恢复位置）。
    服务端存下来，下次登录通过 street_pos_info 下发即可回到原位。
    """
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 2):
        return
    street_id = vals[0]
    pos = vals[1]
    if not isinstance(pos, dict):
        return
    st = state(uid)
    if int(st.props.get("street_id") or 0) != int(street_id or 0):
        return
    saved = {
        "x": float(pos.get("x") or 0),
        "y": float(pos.get("y") or 0),
        "z": float(pos.get("z") or 0),
        "dir": float(pos.get("dir") or 0),
    }
    st.props["_oo_street_pos"] = saved
    throttled_save(st, uid, force=True)
