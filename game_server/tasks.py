
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.tasks —— 通用任务（TaskData 那一套）状态机。

和 story.py 的区别
------------------
story.py  处理 StoryTaskData（剧情任务，story_task_bag / done_story_tasks）
本模块    处理 TaskData（通用任务，task_bag / done_tasks）

数据结构（逆向依据）
--------------------
avatartaskcomp.lua:256-284：

    function AvatarTaskComp:getTaskState(taskType, taskID)
      local taskServerData = self.task_bag[taskType][taskID]
      if not taskServerData and self.done_tasks[taskType][taskID] then
        return GlobalConst.TaskState.Finished
      end
      if taskServerData then return taskServerData.state end
      return -1
    end

所以：
  task_bag[taskType][taskID]   = TaskItem{id,type,state,get_time,start_time,
                                          doing_targets,finish_targets,cache}
  done_tasks[taskType][taskID] = 完成时间戳（DoneTaskTypeBag 的值类型是 IntDictKeyInt）

类型（generated/config.lua）
  task_bag    : TaskTypeBag  = Dict<int, TaskBag>，TaskBag = Dict<int, TaskItem>
  done_tasks  : DoneTaskTypeBag = Dict<int, IntDictKeyInt>
"""
import time

from . import gamedata
from .story.state_machine import (
    TASK_STATE_CLOSED, TASK_STATE_DOING, TASK_STATE_DONE, TASK_STATE_FAILED,
    TASK_STATE_FINISHED, TASK_STATE_IDLE, TASK_STATE_INIT, TASK_STATE_WAIT_DONE,
    TransitionError, can_transition,
)

TASK_STATE_DOING = 3
TASK_STATE_FINISHED = 6

# 同步描述里的 optype（和客户端 puppetpropertycomponent.lua 一致）
# 由本模块自己决定，不让上层猜 —— 审计发现上层一律用 OP_CHANGE，
# 新建的 task_bag[type] 槽位 / 任务项都发错了。
OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3


def _sync(property_id, optype, name, value):
    """一条属性同步描述。entity_rpc._task_sync_frames 直接照着编帧。"""
    return ("sync", int(property_id or 0), int(optype), str(name), value)

# generated/config.lua:7128 TaskItem
_TASK_ITEM = {
    "id": 0, "type": 0, "state": 0, "get_time": 0, "start_time": 0,
    "doing_targets": {}, "finish_targets": {}, "cache": {},
}


def task_type_of(task_id):
    """TaskData[task_id].TaskType；查不到返回 None。"""
    cfg = gamedata.task(task_id) or {}
    t = cfg.get("TaskType")
    return int(t) if isinstance(t, int) else None


def make_task_item(st, task_id, task_type, state, now):
    item = {"__tp__": "TaskItem", "__id__": st.next_property_id()}
    item.update(_TASK_ITEM)
    # 嵌套的自定义属性必须带 __id__（不带 __tp__），否则客户端 init 会报
    #   class ClientAvatar function init must have a boolean return value
    item["doing_targets"] = {"__id__": st.next_property_id()}
    item["finish_targets"] = {"__id__": st.next_property_id()}
    item["cache"] = {"__id__": st.next_property_id()}
    item["id"] = int(task_id)
    item["type"] = int(task_type)
    item["state"] = int(state)
    item["get_time"] = int(now)
    item["start_time"] = int(now)
    return item


def ensure_type_slot(st, task_type):
    """保证 task_bag[taskType] 这个 TaskBag 存在，返回 (slot, is_new)。

    ★ is_new 很重要：客户端 getSystemTaskState 会先看 task_bag[taskType]
      是否存在，为空直接返回 -1。所以新建槽位必须**单独发一条 OP_ADD**，
      否则客户端永远看不到这个槽位，任务状态恒 -1。
    """
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return None, False
    slot = bag.get(task_type)
    if isinstance(slot, dict):
        return slot, False
    slot = {"__tp__": "TaskBag", "__id__": st.next_property_id()}
    bag[task_type] = slot
    st.set("task_bag", bag)
    return slot, True


def task_state(props, task_id):
    """-1 = 没有；否则返回 state（Finished 也算 6）。"""
    t = task_type_of(task_id)
    if t is None:
        return -1
    bag = props.get("task_bag")
    if isinstance(bag, dict):
        slot = bag.get(t)
        if isinstance(slot, dict):
            it = slot.get(task_id)
            if isinstance(it, dict):
                return int(it.get("state") or 0)
    done = props.get("done_tasks")
    if isinstance(done, dict):
        dslot = done.get(t)
        if isinstance(dslot, dict) and dslot.get(task_id) is not None:
            return TASK_STATE_FINISHED
    return -1


def is_finished(props, task_id):
    return task_state(props, task_id) == TASK_STATE_FINISHED


def start_task(st, task_id, now=None):
    """把任务设为进行中。返回同步项列表。"""
    now = int(now if now is not None else time.time())
    t = task_type_of(task_id)
    if t is None:
        return []
    if task_state(st.props, task_id) in (TASK_STATE_DOING, TASK_STATE_FINISHED):
        return []
    slot, slot_is_new = ensure_type_slot(st, t)
    if slot is None:
        return []
    bag = st.props.get("task_bag") or {}
    out = []
    if slot_is_new:
        # 新槽位：先让客户端建出 task_bag[t]
        out.append(_sync(bag.get("__id__"), OP_ADD, t, slot))
    item_is_new = task_id not in slot
    if item_is_new:
        # ★ P1 阶段 F：新任务必须先 OP_ADD 建 Init(2) 条目，再 OP_CHANGE 到 Doing(3)。
        #   以前直接建 Doing 并只发 OP_ADD —— 而客户端 config.lua:10123-10126 的
        #   changed["task_bag.*.*"] -> onTaskItemChanged 只对 OP_CHANGE 触发，
        #   entryAdded["task_bag.*"] -> onTaskBagInserted 是空函数。
        #   所以「接任务」这个动作在客户端一点反应都没有（任务面板不刷新）。
        item = make_task_item(st, task_id, t, TASK_STATE_INIT, now)
        slot[task_id] = item
        st.set("task_bag", st.props.get("task_bag"))
        out.append(_sync(slot.get("__id__"), OP_ADD, task_id, item))
        # Init -> Doing：触发 onTaskItemChanged
        item["state"] = TASK_STATE_DOING
        st.set("task_bag", st.props.get("task_bag"))
        out.append(_sync(slot.get("__id__"), OP_CHANGE, task_id, item))
    else:
        # 已在包里：只做状态迁移（Done -> Doing 之类）
        item = slot[task_id]
        cur = int(item.get("state") or 0)
        if cur not in (TASK_STATE_DOING, TASK_STATE_FINISHED):
            if can_transition(cur, TASK_STATE_DOING):
                item["state"] = TASK_STATE_DOING
                st.set("task_bag", st.props.get("task_bag"))
                out.append(_sync(slot.get("__id__"), OP_CHANGE, task_id, item))
    return out


def finish_task(st, task_id, now=None):
    """标记任务完成。返回同步项列表。"""
    now = int(now if now is not None else time.time())
    t = task_type_of(task_id)
    if t is None:
        return []
    out = []
    bag = st.props.get("task_bag")
    if isinstance(bag, dict):
        slot = bag.get(t)
        if isinstance(slot, dict) and task_id in slot:
            # ★ P1 阶段 F：完成也要走迁移 —— 先 OP_CHANGE 到 Finished(6)，
            #   再 OP_DEL 出包。以前直接 DEL，客户端 onTaskItemChanged 不触发，
            #   「任务完成」在 UI 上没有任何反馈。
            item = slot[task_id]
            cur = int(item.get("state") or 0)
            if cur != TASK_STATE_FINISHED and can_transition(cur, TASK_STATE_FINISHED):
                item["state"] = TASK_STATE_FINISHED
                st.set("task_bag", bag)
                out.append(_sync(slot.get("__id__"), OP_CHANGE, task_id, item))
            del slot[task_id]
            st.set("task_bag", bag)
            out.append(_sync(slot.get("__id__"), OP_DEL, task_id, None))
    done = st.props.get("done_tasks")
    if isinstance(done, dict):
        dslot = done.get(t)
        dslot_is_new = not isinstance(dslot, dict)
        if dslot_is_new:
            dslot = {"__id__": st.next_property_id()}
            done[t] = dslot
        item_is_new = task_id not in dslot
        dslot[task_id] = now
        st.set("done_tasks", done)
        if dslot_is_new:
            out.append(_sync(done.get("__id__"), OP_ADD, t, dslot))
        out.append(_sync(dslot.get("__id__"),
                         OP_ADD if item_is_new else OP_CHANGE,
                         task_id, now))
    return out


def tasks_of_type(st, task_type):
    """当前进行中的某类型任务 id 列表。"""
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return []
    slot = bag.get(task_type)
    if not isinstance(slot, dict):
        return []
    return [k for k in slot if isinstance(k, int)]


# ---------------------------------------------------------------- N-a2 播种

def _all_task_ids():
    """TaskData 全部 uiD（升序，保证可复现）。"""
    d = gamedata.table("TaskData") or {}
    out = []
    for v in (d.values() if isinstance(d, dict) else d):
        if isinstance(v, dict) and v.get("uiD") is not None:
            try:
                out.append(int(v["uiD"]))
            except Exception:
                pass
    return sorted(out)


def pre_satisfied(st, task_id):
    """PreTask 是否满足。

    ★ PreTaskType 语义【实测，两张表一致】：
        1 -> 前置在 TaskData
        2 -> 前置在 StoryTaskData
      核对数据：StoryTaskData 958 条 =2 全在 StoryTaskData；5 条 =1 全在 TaskData；
               TaskData 145 条 =1 全在 TaskData；35 条 =2 全在 StoryTaskData。
    """
    cfg = gamedata.task(task_id) or {}
    pre = cfg.get("PreTask")
    if pre is None:
        return True
    try:
        pre = int(pre)
    except Exception:
        return False
    pt = cfg.get("PreTaskType") or 1
    try:
        pt = int(pt)
    except Exception:
        pt = 1
    if pt == 2:
        from . import story
        return story.story_state(st.props, pre) == TASK_STATE_FINISHED
    return task_state(st.props, pre) == TASK_STATE_FINISHED


def seed_tasks(st, now=None, limit=400):
    """把「前置满足 + 不在包 + 未完成」的通用任务播进 task_bag。

    返回同步描述列表（格式同 start_task，交给 story/tasks.py:_task_sync_frames 编帧）。

    ★ 为什么必须播种（P1 阶段 N-a2）
      之前 task_bag 实测 0 槽位、0 任务 —— 通用任务一条都不存在，于是：
        getSystemTaskState(任意) 恒 -1
        -> AllSystemCatalogueData 里 40 条 unlockType=1 的系统入口数据条件永假
      见 docs/P1-剧情任务状态机.md 阶段 N 的 N2.1 ④。
    """
    now = int(now if now is not None else time.time())
    out = []
    seeded = 0
    cands = []
    for tid in _all_task_ids():
        if task_state(st.props, tid) != -1:
            continue
        if not pre_satisfied(st, tid):
            continue
        cands.append(tid)
    for tid in cands:
        if seeded >= limit:
            print("[task] 播种达到上限 %d，还剩 %d 个候选未播"
                  % (limit, len(cands) - seeded), flush=True)
            break
        ch = start_task(st, tid, now)
        if ch:
            out.extend(ch)
            seeded += 1
    if seeded:
        print("[task] 通用任务播种：%d 个进包（候选 %d 个）" % (seeded, len(cands)),
              flush=True)
    return out


# ---------------------------------------------------------------- N-a3 进度推进

def evaluate_event(st, event_type, params):
    """把一条游戏事件喂给通用任务，推进 doing_targets。

    返回 (completed_ids, sync_changes)：
      completed_ids  已达到完成条件的 taskId（调用方负责 finish_task）
      sync_changes   进度变化的同步描述

    客户端依据（avatartaskcomp.lua:23-45 / directortitlehelper + 面板）：
      doing_targets = {eventId: 已完成次数}，进度 = 求和，分母 = TaskData.TriggerTimes[0]。
    """
    from .story import event_matches
    done_ids = []
    syncs = []
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return done_ids, syncs
    for tt, slot in list(bag.items()):
        if not isinstance(tt, int) or not isinstance(slot, dict):
            continue
        for tid, item in list(slot.items()):
            if not isinstance(tid, int) or not isinstance(item, dict):
                continue
            if int(item.get("state") or 0) != TASK_STATE_DOING:
                continue
            cfg = gamedata.task(tid) or {}
            evs = cfg.get("TriggerEventList") or []
            times = cfg.get("TriggerTimes") or [1]
            try:
                need = int(times[0]) if times else 1
            except Exception:
                need = 1
            hit_eid = None
            for eid in evs:
                if event_matches(gamedata.task_event(eid), event_type, params):
                    try:
                        hit_eid = int(eid)
                    except Exception:
                        hit_eid = None
                    break
            if hit_eid is None:
                continue
            dt = item.get("doing_targets")
            if not isinstance(dt, dict):
                dt = {"__id__": st.next_property_id()}
                item["doing_targets"] = dt
            cur = int(dt.get(hit_eid) or 0)
            dt[hit_eid] = cur + 1
            total = sum(int(v) for k, v in dt.items() if isinstance(k, int))
            syncs.append(_sync(slot.get("__id__"), OP_CHANGE, tid, item))
            if total >= need:
                done_ids.append(tid)
    if syncs:
        st.set("task_bag", st.props.get("task_bag"))
    return done_ids, syncs
