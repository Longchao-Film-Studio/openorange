
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

TASK_STATE_DOING = 3
TASK_STATE_FINISHED = 6

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
    """保证 task_bag[taskType] 这个 TaskBag 存在，返回它。"""
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return None
    slot = bag.get(task_type)
    if isinstance(slot, dict):
        return slot
    slot = {"__tp__": "TaskBag", "__id__": st.next_property_id()}
    bag[task_type] = slot
    st.set("task_bag", bag)
    return slot


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
    slot = ensure_type_slot(st, t)
    if slot is None:
        return []
    item = make_task_item(st, task_id, t, TASK_STATE_DOING, now)
    slot[task_id] = item
    st.set("task_bag", st.props.get("task_bag"))
    return [("sub2", "task_bag", t, task_id, item)]


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
            del slot[task_id]
            st.set("task_bag", bag)
            out.append(("del2", "task_bag", t, task_id, None))
    done = st.props.get("done_tasks")
    if isinstance(done, dict):
        dslot = done.get(t)
        if not isinstance(dslot, dict):
            dslot = {"__id__": st.next_property_id()}
            done[t] = dslot
        dslot[task_id] = now
        st.set("done_tasks", done)
        out.append(("sub2", "done_tasks", t, task_id, now))
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
