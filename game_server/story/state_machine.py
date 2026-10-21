#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.story.state_machine —— 剧情任务状态机（唯一写入口）。

★ P1 阶段 A：为什么要有这个模块

  以前任务状态是「哪里需要哪里直接赋值」：
    - start_story_task 直接建一条 state=Doing(3) 的条目，发 OP_ADD
    - finish_story_task 直接建一条 state=Finished(6) 的条目写进 done，发 OP_ADD+OP_DEL
  结果是**从来没有一条 story_task_bag.<id> 的 OP_CHANGE**，
  而客户端 5 类 UI（街区 NPC 刷新 / 场记板 / 章节提示 / 红点 / 战斗事件）
  全部挂在 changed["story_task_bag.*"] -> onStoryTaskItemChanged 上
  （generated/config.lua:9931-9956）。于是任务做完了，界面一点反应都没有。

★ 不变量（违反即 bug）

  1. 一条任务的状态只能沿 Init(2) → Doing(3) → WaitDone(4) → Done(5) → Finished(6)
     单向前进；表外组合一律抛 TransitionError。
  2. 每次状态变化产生**且只产生一条** ("sub", "story_task_bag", taskId, item) 同步描述，
     由 story.rpc._story_sync_frames 编成 OP_CHANGE。
  3. 只有 Finished(6) 的任务能被归档进 done_story_tasks。
  4. 本模块是全仓**唯一**允许修改 story_task_bag[*]["state"] 的地方
     （tests/test_story_state_machine.py 用 AST 检查这一条）。

★ 依据（客户端）

  common/globalconst.lua:77-86  TaskState = {Idle=1, Init=2, Doing=3, WaitDone=4,
                                             Done=5, Finished=6, Failed=-1, Closed=-2}
  avatarstorytaskcomp.lua:409-434  old.state < Doing and new.state == Doing -> 接任务 UI
  avatarstorytaskcomp.lua:376-435  old.state ~= Finished and new.state == Finished -> 场记板
  avatarstorytaskcomp.lua:557-558  onStoryTaskBagInserted 是空函数（OP_ADD 不产生 UI）
"""

TASK_STATE_IDLE = 1
TASK_STATE_INIT = 2
TASK_STATE_DOING = 3
TASK_STATE_WAIT_DONE = 4
TASK_STATE_DONE = 5
TASK_STATE_FINISHED = 6
TASK_STATE_FAILED = -1
TASK_STATE_CLOSED = -2

_STATE_NAME = {
    TASK_STATE_IDLE: "Idle",
    TASK_STATE_INIT: "Init",
    TASK_STATE_DOING: "Doing",
    TASK_STATE_WAIT_DONE: "WaitDone",
    TASK_STATE_DONE: "Done",
    TASK_STATE_FINISHED: "Finished",
    TASK_STATE_FAILED: "Failed",
    TASK_STATE_CLOSED: "Closed",
}


def state_name(state):
    return _STATE_NAME.get(state, str(state))


# 自定义类型形状（generated/config.lua:6698 StoryTaskItem）
# 嵌套的 IntDictKeyInt / IntList **必须带 __id__**、不带 __tp__
# （puppetpropertycomponent.lua:161-175：嵌套对象靠 __id__ 定位）
STORY_TASK_ITEM_FIELDS = {
    "id": 0, "state": 0, "get_time": 0, "start_time": 0,
    "doing_targets": {}, "finish_targets": {}, "finish_parameter": {},
}


def make_story_task_item(st, task_id, state, now):
    """建一条 StoryTaskItem（带 __tp__ / __id__ / 三个嵌套容器的 __id__）。"""
    item = {"__tp__": "StoryTaskItem", "__id__": st.next_property_id()}
    item.update(STORY_TASK_ITEM_FIELDS)
    item["doing_targets"] = {"__id__": st.next_property_id()}
    item["finish_targets"] = {"__id__": st.next_property_id()}
    item["finish_parameter"] = {"__id__": st.next_property_id()}
    item["id"] = int(task_id)
    item["state"] = int(state)
    item["get_time"] = int(now)
    item["start_time"] = int(now)
    return item


# ---------------------------------------------------------------------------
# 迁移表
# ---------------------------------------------------------------------------
# key = (from, to)，value = 迁移名（写进日志 / 测试断言用）
#
# 为什么 INIT -> FINISHED 也合法：type0（270 条「副本内目标」）没有完成条件，
# 播种后要能直接归档，否则会永远卡在 Init。
# 为什么 DOING -> FINISHED 也合法：绝大多数剧情任务（客户端上报型）没有
# WaitDone/Done 这两步 —— 客户端 task_bag 那套才用 4/5。
TRANSITIONS = {
    (TASK_STATE_IDLE, TASK_STATE_INIT): "create",
    (TASK_STATE_INIT, TASK_STATE_DOING): "begin",
    (TASK_STATE_DOING, TASK_STATE_WAIT_DONE): "target_done",
    (TASK_STATE_WAIT_DONE, TASK_STATE_DONE): "deliver",
    (TASK_STATE_DOING, TASK_STATE_FINISHED): "finish",
    (TASK_STATE_WAIT_DONE, TASK_STATE_FINISHED): "finish",
    (TASK_STATE_DONE, TASK_STATE_FINISHED): "finish",
    (TASK_STATE_INIT, TASK_STATE_FINISHED): "finish_skip",
}


class TransitionError(ValueError):
    """非法迁移：表里没有这个 (from, to)。"""


def can_transition(frm, to):
    return (frm, to) in TRANSITIONS


def transition_name(frm, to):
    return TRANSITIONS.get((frm, to))


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _bag(props):
    v = props.get("story_task_bag")
    return v if isinstance(v, dict) else None


def _done(props):
    v = props.get("done_story_tasks")
    return v if isinstance(v, dict) else None


def current_state(props, task_id):
    """-1 = 不在包里也不在 done；否则返回 state（done 里一律视为 Finished）。"""
    bag = _bag(props)
    if bag is not None:
        it = bag.get(int(task_id))
        if isinstance(it, dict):
            try:
                return int(it.get("state") or 0)
            except Exception:
                return 0
    done = _done(props)
    if done is not None and isinstance(done.get(int(task_id)), dict):
        return TASK_STATE_FINISHED
    return -1


def create_task(st, task_id, now):
    """建一条 Init(2) 任务进包。已在包里或已归档则返回空列表。

    返回 [("add", "story_task_bag", taskId, item)] —— 由 _story_sync_frames 编成 OP_ADD。
    ★ OP_ADD 本身不会触发客户端任何回调（onStoryTaskBagInserted 是空函数），
      所以建完必须再 transition 到 Doing 才会有 UI 反应。
    """
    task_id = int(task_id)
    props = st.props
    bag = _bag(props)
    if bag is None:
        return []
    if task_id in bag:
        return []
    done = _done(props)
    if done is not None and task_id in done:
        return []
    item = make_story_task_item(st, task_id, TASK_STATE_INIT, now)
    bag[task_id] = item
    st.set("story_task_bag", bag)
    return [("add", "story_task_bag", task_id, item)]


def transition(st, task_id, to_state, source="", now=None):
    """把一个已在包里的任务迁到 to_state，返回同步描述列表。

    唯一允许改 story_task_bag[*]["state"] 的函数。
    - 同状态重复迁移：幂等，返回 []
    - 表外组合：抛 TransitionError（带 from/to 名字，便于定位）
    - 任务不在包里：抛 TransitionError（必须先 create_task）
    """
    task_id = int(task_id)
    to_state = int(to_state)
    props = st.props
    bag = _bag(props)
    if bag is None:
        raise TransitionError("story_task_bag 不存在，无法迁移 task %d" % task_id)
    item = bag.get(task_id)
    if not isinstance(item, dict):
        raise TransitionError(
            "task %d 不在 story_task_bag 里（当前状态 %s），不能迁移到 %s"
            % (task_id, current_state(props, task_id), state_name(to_state)))
    frm = int(item.get("state") or 0)
    if frm == to_state:
        return []
    if not can_transition(frm, to_state):
        raise TransitionError(
            "非法迁移 task %d: %s(%d) -> %s(%d)，来源=%s"
            % (task_id, state_name(frm), frm, state_name(to_state), to_state,
               source or "?"))
    item["state"] = to_state
    st.set("story_task_bag", bag)
    return [("sub", "story_task_bag", task_id, item)]


def move_to_done(st, task_id):
    """把一条**已 Finished** 的任务从包里挪进 done_story_tasks。

    返回 [("add", "done_story_tasks", taskId, item), ("del", "story_task_bag", taskId, None)]。
    ★ 顺序不能反：先 OP_CHANGE 到 Finished（在 transition 里），再 add done、del bag。
      反过来的话客户端永远观察不到 old.state -> 6 的转换，场记板/章节结束不会出现。
    """
    task_id = int(task_id)
    props = st.props
    bag = _bag(props)
    done = _done(props)
    if bag is None or done is None:
        return []
    item = bag.get(task_id)
    if not isinstance(item, dict):
        return []
    frm = int(item.get("state") or 0)
    if frm != TASK_STATE_FINISHED:
        raise TransitionError(
            "task %d 还没 Finished（当前 %s），不能归档"
            % (task_id, state_name(frm)))
    item["state"] = TASK_STATE_FINISHED
    done[task_id] = item
    bag.pop(task_id, None)
    st.set("done_story_tasks", done)
    st.set("story_task_bag", bag)
    return [("add", "done_story_tasks", task_id, item),
            ("del", "story_task_bag", task_id, None)]
