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
from ..rpc_common import (
    SYNC_SELF, decode_params, respond_after_sync, state, sync_frame,
    throttled_save)
from .info import _make_street_info_entry
def _switch_street(rid, params, uid=None):
    """RequestSwitchStreet_CS {streetId, streetParams} / RpcEnterStreet {id, streetParams}。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        new_id = int(vals[0])
    except Exception:
        return respond_after_sync(rid, syncs)
    syncs = _apply_street_change(new_id, uid) or []
    return respond_after_sync(rid, syncs)

def _leave_street(rid, params, uid=None):
    """RequestLeaveStreet_CS {id} —— 离开街区，street_id 归 0。"""
    syncs = _apply_street_change(0, uid) or []
    return respond_after_sync(rid, syncs)

def _refresh_trace_on_street_change(st, new_id, syncs):
    """★ 切街区后重算追踪目标，并强制客户端刷新标点。

    实机反馈（2026-10-22）：「跟随任务指引到了下一个场景之后，
    任务还是在上一个场景标点，重过它还是一直在上一个的标点」。

    客户端证据：
      ui/panel/streetv2panel/streettasktrackcomp.lua:14
        标点刷新只绑在 EStreetTaskTracking 事件上（属性 OP_CHANGE 才触发）
      :90,99-106  getStreetActorByUniqName 只在**当前场景**找 NPC，
                  找不到就 setTrackingInfo(nil) -> 标点停在原地不动

    所以切街区必须重算一次：
      1. 玩家上次想追踪的任务（_oo_trace_wanted）如果在新街区可做 -> 恢复它
      2. 否则找一个「在新街区、正在做」的任务（按同章节 > 主线 > id 排序）
      3. 都没有 -> 0（客户端 clearAllTrackingSign，不留错误标点）
      4. 无论值变不变都推一次 OP_CHANGE（否则客户端不刷新）
    """
    from .interact import _effective_trace   # 局部导入避免循环依赖

    try:
        wanted = int(st.props.get("_oo_trace_wanted") or 0)
    except Exception:
        wanted = 0
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        bag = {}

    def _doing(t):
        it = bag.get(t)
        return isinstance(it, dict) and it.get("state") == story.TASK_STATE_DOING

    def _scene_of(t):
        gs = (gamedata.story_task(t) or {}).get("GuideSceneId")
        try:
            return int(gs) if gs is not None else None
        except Exception:
            return None

    # 1) 想追踪的任务在新街区可做吗
    nxt = _effective_trace(st, wanted)

    # 2) 不可做 -> 找新街区里正在做的任务
    if not nxt and new_id:
        cur_cfg = gamedata.story_task(wanted) or {} if wanted else {}
        cur_chapter = cur_cfg.get("ChapterId")
        cands = [int(t) for t in bag
                 if str(t).lstrip("-").isdigit()
                 and _doing(int(t))
                 and _scene_of(int(t)) == int(new_id)]

        def _rank(t):
            ch = (gamedata.story_task(t) or {}).get("ChapterId")
            same = 0 if (cur_chapter and ch == cur_chapter) else 1
            is_main = 0 if str(ch or "").startswith("main") else 1
            return (same, is_main, t)

        if cands:
            nxt = sorted(cands, key=_rank)[0]
            print("[street] 追踪目标 %s 不在街区 %s，切到 %s"
                  % (wanted, new_id, nxt), flush=True)

    # 3) 都没有 -> 0
    st.props["street_trace_taskid"] = nxt
    # 4) 总是推一次（值没变也要推，客户端靠它刷新标点）
    syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                            "street_trace_taskid", nxt))


def _apply_street_change(new_id, uid):
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
    syncs = list(tx.frames())
    # ★ 阶段 P：切街到达目标街 -> 完成 type4 剧情任务（唯一正确路径）。
    #   客户端证据见 docs/storytaskresearch.md §16 / §19：
    #     - 13 条 type4 的 Parameter1 全是「目标街区 id」
    #     - 能验证的 4 条 transferStreet 与 Parameter1 4/4 一致
    #     - 客户端不上报 type4，必须服务端在切街时自己判
    #   实机断点：1045「回龙潮阁复命」Parameter1=[100]，玩家从 101 切到 100
    #   时应该完成它，之后 1004 才能进包 -> actor 40005 -> playTimeline 7。
    try:
        from ..story import street_arrival_tasks
        from ..story.rpc import _finish_story_tasks
        _hits = street_arrival_tasks(st, new_id)
        if _hits:
            syncs.extend(_finish_story_tasks(st, _hits, source="street_arrival") or [])
    except Exception as _e:
        print("[street] 切街完成 type4 失败: %s" % _e, flush=True)
    # ★ 切街区后刷新追踪标点（否则标点留在旧街区，玩家找不到下一步）
    _refresh_trace_on_street_change(st, new_id, syncs)
    print("[street] %s -> %s" % (old, new_id), flush=True)
    return syncs

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
