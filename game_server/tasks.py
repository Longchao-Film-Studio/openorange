
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
