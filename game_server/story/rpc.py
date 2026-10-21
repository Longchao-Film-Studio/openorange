#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剧情任务的 RPC 入口与落状态（P0-E2 第 2 步）。

落状态的**唯一**入口是 _finish_story_tasks：
  - story.events / story.check_chain_tasks 只做「检测」，返回命中的 taskId；
  - 这里才真正调 story.finish_story_task（写 story_task_bag / done_story_tasks）、
    发奖、发 GetItem 事件、重算系统解锁位。
踩过的坑：如果在检测处直接落状态，外层再调这里会因为「已经是 Finished」
而跳过，导致不发奖、不推同步帧。
"""

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 剧情任务推进（阶段 2 核心）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstorytaskcomp.lua:
#   getStoryTaskState(taskID)  读 story_task_bag[taskID].state / done_story_tasks
#   getTaskIdByParamInt        遍历 story_task_bag 找 TaskType+Parameter1 命中的
#   RequestDoneStoryTaskParamInt_CS {taskId, param_int}
#   RequestDoneStoryTaskParamStr_CS {taskId, param_str}
#   RequestStreetActor_CS {actor_id}  = RequestDoneStoryTaskParamInt_CS(2, actor_id)
#     + serverRpc("RequestStreetActor_CS")  ← 街景 NPC 交互就是剧情推进

import time

from .. import gamedata
from .. import ms as msmod
from .. import story
from ..rpc_common import SYNC_SELF, EVENT_MAX_DEPTH, decode_params, state, sync_frame
from ..rpc_index import ACK_OK
from . import events as _events

def _story_sync_frames(st, changes):
    """把 story 模块的同步描述转成 propertySync 帧。"""
    frames = []
    for ch in changes:
        kind = ch[0]
        if kind == "root":
            _, name, value = ch
            frames.append(sync_frame(
                SYNC_SELF, 0, msmod.OP_CHANGE, name, value))
        elif kind == "add":
            # ★ 给自定义 dict **加新键**必须用 OP_ADD（客户端 __OpAdd__），
            #   用 OP_CHANGE 会走 __OpChange__，那里 oldVal 为 nil 直接报
            #   "subproperty is nil" 并把这次变更丢掉。
            #   实测症状：剧情任务完成后 story_task_bag 删掉了，
            #   但 done_story_tasks 里没有 -> 客户端 getTaskStateByTaskID 返回 -1。
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_ADD, str(key), value))
        elif kind == "sub":
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_CHANGE, str(key), value))
        elif kind == "child":
            # 改「某个条目内部的子属性」（例如 story_task_bag.<id>.finish_parameter）：
            # propertyId 是**条目自己的 __id__**，name 是子属性名。
            # 客户端 config.lua:9941-9944 的 changed["story_task_bag.*.finish_parameter"]
            # 就是靠这个路径命中的。
            _, obj, name, value = ch
            if not isinstance(obj, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, obj.get("__id__") or 0, msmod.OP_CHANGE, str(name), value))
        elif kind == "del":
            _, container, key, _v = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_DEL, str(key), None))
    return frames

def _recalc_story_unlocks(st):
    """按 AllSystemCatalogueData 的 unlockType=2(StoryTask) 重算系统解锁位。

    客户端判定在 ui/panel/streetv2panel/node_menu_item/node_menu_itemgroup.lua:231：
      unlock = getTaskStateByTaskID(taskId) == Finished
               and isSysEntryUnlock(sysId)
    后者读 system_p.unlocked_system[sysId]，所以服务端两样都要维护。
    """
    frames = []
    sp = st.props.get("system_p")
    if not isinstance(sp, dict):
        return frames
    us = sp.get("unlocked_system")
    if not isinstance(us, dict):
        return frames
    us_id = us.get("__id__") or 0
    changed = []
    for row in gamedata.all_system_catalogue():
        if row.get("unlockType") != 2:
            continue
        tid = row.get("unlockParam")
        sid = row.get("id")
        if not isinstance(tid, int) or not isinstance(sid, int):
            continue
        if us.get(sid) is True:
            continue
        if story.story_state(st.props, tid) != story.TASK_STATE_FINISHED:
            continue
        us[sid] = True
        changed.append(sid)
    if changed:
        st.set("system_p", sp)
        for sid in changed:
            frames.append(sync_frame(
                SYNC_SELF, us_id, msmod.OP_CHANGE, str(sid), True))
        print("[story] 解锁系统 %s" % changed, flush=True)
    return frames

def _emit_get_item(st, item_id, count):
    """发一条 GetItem 事件（覆盖 4 条剧情任务）。

    ★ 审计 P0-2：不能依赖客户端 RecordItemObtain_CS ——
      客户端只在 ItemData.firstObtainNotice 为真且首次获取时才发，
      全表只有 6 个道具有这个标记，而剧情任务要的 701/118 都没有。
      所以服务端在真正发道具的地方主动发事件。

    参数尽量补全：TaskEventData 里 GetItem 的 KeyList 有
      [itemId] / [itemType,level,grade] / [itemType,grade] /
      [itemType,itemSubType] / [itemType] 五种，
    能查到 ItemData 就把 itemType/grade/level/itemSubType 都带上。
    """
    if not item_id:
        return []
    item_type = grade = level = sub_type = None
    try:
        info = gamedata.item(item_id) or {}
        for name, box in (("itemType", "t"), ("grade", "g"),
                          ("level", "l"), ("itemSubType", "s")):
            v = info.get(name)
            if isinstance(v, int):
                if box == "t":
                    item_type = v
                elif box == "g":
                    grade = v
                elif box == "l":
                    level = v
                else:
                    sub_type = v
    except Exception:
        pass
    ev = _events.ev_get_item(int(item_id), int(count or 1), item_type, level,
                             grade)
    if sub_type is not None:
        ev.params["itemSubType"] = sub_type
    try:
        res = _events.apply_event(st, ev)
        return list(res.hits) + list(res.chain_hits)
    except Exception as e:
        print("[story] GetItem 事件失败 %s: %s" % (item_id, e), flush=True)
        return []

def _finish_story_tasks(st, task_ids, depth=0):
    """完成若干剧情任务，返回全部同步帧。

    ★ P0-D：完成之后会发一条 FinishTask 事件进事件总线。
      depth 是递归保护：任务图理论上无环，但外部数据不一定可靠。

    ★ 审计 P1-4：save 放进 finally —— 之前只在 depth==0 的**正常返回**路径
      调 save，递归里抛异常就完全不落盘，内存改了但盘上没改。
    """
    frames = []
    now = int(time.time())
    finished_now = []
    try:
        return _finish_story_tasks_inner(st, task_ids, depth, frames, now,
                                         finished_now)
    finally:
        if depth == 0:
            try:
                st.save()
            except Exception as e:
                print("[story] 保存失败: %s" % e, flush=True)

def _finish_story_tasks_inner(st, task_ids, depth, frames, now, finished_now):
    done_bag = st.props.get("done_story_tasks")
    if not isinstance(done_bag, dict):
        done_bag = {}
    for tid in task_ids:
        # ★ 审计 P0-2：判「是否真完成」要看**有没有归档进 done**，不能只看 state==6。
        #   否则「标记完成 -> 发奖失败」留下的 bag-state-6 任务会被永远跳过，
        #   既不会重试发奖，也不会归档。
        if isinstance(done_bag.get(int(tid)), dict):
            continue
        # ★ P1 阶段 A：完成拆成三步，发奖插在中间
        #   1) →Finished(6)：一条 OP_CHANGE，客户端靠它触发场记板/章节提示/清红点
        #   2) 发奖：失败时任务仍在包里且状态 Finished，可重试（不会静默丢奖励）
        #   3) 归档 + 推进 PostTasks
        frames.extend(_story_sync_frames(
            st, story.mark_story_task_finished(st, tid, now)))
        # 发奖励（新格子用 OP_ADD，已有格子用 OP_CHANGE）
        for award in story.story_award(st, tid):
            _item_id, _cnt, grid_key, is_new = award
            # ★ P0-D：服务端发 GetItem 事件（审计 P0-2：
            #   客户端只在 6 个 firstObtainNotice 道具首次获取时才发
            #   RecordItemObtain_CS，任务要的 701/118 永远等不到 RPC，
            #   所以必须服务端自己发）
            _get_hits = _emit_get_item(st, _item_id, _cnt)
            if _get_hits and depth < EVENT_MAX_DEPTH:
                frames.extend(_finish_story_tasks(st, _get_hits, depth + 1))
            bag = st.props.get("item_bag")
            if not isinstance(bag, dict):
                break
            grid = bag.get(grid_key)
            if isinstance(grid, dict):
                frames.append(sync_frame(
                    SYNC_SELF, bag.get("__id__") or 0,
                    msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                    str(grid_key), grid))
        frames.extend(_story_sync_frames(
            st, story.archive_story_task(st, tid, now)))
        print("[story] 完成剧情任务 %s" % tid, flush=True)
        finished_now.append(tid)
    # ★ P0-D：把「完成了哪些任务」作为事件发出去。
    #   审计 P1-2 实测：116 条 FinishTask 事件里**没有任何一条**被
    #   TaskType 3/7 的剧情任务引用，所以这段对剧情任务 0 命中；
    #   真正起作用的是下面 _recalc_story_unlocks + 外层 events.tick 的链式检查。
    #   保留它是为了将来数据里出现 FinishTask 引用时能自动生效。
    if finished_now and depth < EVENT_MAX_DEPTH:
        for tid in finished_now:
            try:
                res = _events.apply_event(
                    st, _events.ev_finish_task(tid, source="internal"))
                nxt = list(res.hits) + list(res.chain_hits)
                if nxt:
                    frames.extend(_finish_story_tasks(st, nxt, depth + 1))
            except Exception as e:
                print("[story] FinishTask 事件失败 %s: %s" % (tid, e), flush=True)
    elif finished_now:
        print("[story] 事件链超过 %d 层，停止递归（任务 %s）"
              % (EVENT_MAX_DEPTH, finished_now[:5]), flush=True)
    frames.extend(_recalc_story_unlocks(st))
    return frames

def _accumulate_multi_target(st, tid, param, frames):
    """多目标任务：先把 param 记进 finish_parameter；没集齐返回 False（暂不完成）。

    ★ 数据依据：StoryTaskData 里 Parameter1 多于 1 项的任务共 11 条（8 条带 GuideParam）。
      客户端每完成一个目标就发一次 RequestDoneStoryTaskParamInt_CS(taskId, param)，
      而服务端以前收到第一个就把整个任务完成了 —— 剩下两个目标被直接吞掉，
      追踪箭头也会提前全消失（streettasktrackcomp.lua:108-134 靠 finish_parameter 判断）。
    """
    cfg = gamedata.story_task(tid) or {}
    p1 = cfg.get("Parameter1") or []
    if not isinstance(p1, list) or len(p1) <= 1 or param is None:
        return True
    frames.extend(_story_sync_frames(
        st, story.mark_finish_parameter(st, tid, param)))
    bag = st.props.get("story_task_bag") or {}
    item = bag.get(int(tid))
    got = story.finish_parameters_of(item)
    return all(int(x) in got for x in p1)


def _done_story_task(rid, params, uid=None):
    """RequestDoneStoryTaskParamInt_CS / ParamStr_CS —— 参数第 0 项是 taskId。"""
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        tid = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    param = None
    if len(vals) > 1:
        try:
            param = int(vals[1])
        except Exception:
            param = None
    if not _accumulate_multi_target(st, tid, param, frames):
        return frames
    frames.extend(_finish_story_tasks(st, [tid]))
    return frames

def _street_actor(rid, params, uid=None):
    """RequestStreetActor_CS {actor_id} —— 街景 NPC 交互。

    客户端 avatarstreetcomp.lua:339-342 先调
      RequestDoneStoryTaskParamInt_CS(2, actor_id)
    再调 serverRpc("RequestStreetActor_CS", {actor_id})。
    这里按同样的规则找命中任务：story_task_bag 里 TaskType==2 且 Parameter1 含 actor_id。
    """
    frames = [msmod.build_entity_response(rid, ret=dict(ACK_OK))]
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return frames
    try:
        actor_id = int(vals[0])
    except Exception:
        return frames
    st = state(uid)
    hits = _find_story_tasks_by_param(st, actor_id, 2)
    ready = [tid for tid in hits
             if _accumulate_multi_target(st, tid, actor_id, frames)]
    if ready:
        frames.extend(_finish_story_tasks(st, ready))
    return frames

def _find_story_tasks_by_param(st, param_int, task_type):
    """照 getTaskIdByParamInt 的规则找任务。"""
    out = []
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        return out
    for tid, item in bag.items():
        if not isinstance(item, dict):
            continue
        cfg = gamedata.story_task(tid) or {}
        if cfg.get("TaskType") != task_type:
            continue
        p1 = cfg.get("Parameter1") or []
        if param_int in p1:
            out.append(int(tid))
    return out
