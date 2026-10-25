
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
    TASK_STATE_DOING, TASK_STATE_DONE, TASK_STATE_FINISHED, TASK_STATE_INIT, TASK_STATE_WAIT_DONE,
    can_transition,
)

TASK_STATE_DOING = 3
TASK_STATE_FINISHED = 6
TASK_STATE_WAIT_DONE = 4   # 目标已达成、等玩家领奖（客户端只把 4 状态发领奖 RPC）
TASK_STATE_DONE = 5        # 已交付

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
def task_type_of(task_id):
    """TaskData[task_id].TaskType；查不到返回 None。"""
    cfg = gamedata.task(task_id) or {}
    t = cfg.get("TaskType")
    return int(t) if isinstance(t, int) else None


def make_task_item(st, task_id, task_type, state, now):
    """建一条 TaskItem（形状由 property.factory 按客户端声明生成）。

    ★ 2026-10-23：字段表和嵌套容器 __id__ 不再手抄，走 factory。
      state 仍在这里写（本函数在 AST 守卫的白名单里）。
    """
    from .property import factory as _factory
    item = _factory.new_instance(st.props, "TaskItem", with_tp=True)
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
    from .property import factory as _factory
    slot = _factory.new_instance(st.props, "TaskBag", with_tp=True)
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


# ---------------------------------------------------------------- N-c 领奖流程

def needs_claim(cfg):
    """这个通用任务需不需要玩家手动领奖。

    判据（客户端证据）：
      - 头衔面板的领奖按钮 `node_challenge_task_itemgroup.lua:151-170`
        只把 state == WaitDone(4) 的任务发进 `RequestDoneTitleTask_CS`；
        也就是说「有奖励的任务」必须停在 WaitDone 等玩家点。
      - `getDirectorTasks` / `sortChanllegeTask` 也用 WaitDone 当「可领取」标记
        （`sortChanllegeTask` 里 `(state - WaitDone) * 10000`）。

    所以：有 `AwardItemList` 且属于头衔类（9/10）的任务 -> 进 WaitDone 等领取；
    其余（含无奖励的 Beginner 任务）-> 直接完成，因为对应面板没有领奖入口
    （Beginner 面板还因数据缺 PanelName 而不可见，见阶段 N 的 N2.1 ①）。
    """
    if not isinstance(cfg, dict):
        return False
    if not cfg.get("AwardItemList"):
        return False
    return cfg.get("TaskType") in (9, 10)


def move_to_wait_done(st, task_id, now=None):
    """Doing(3) -> WaitDone(4)：目标达成，等玩家领奖。返回同步描述。"""
    task_id = int(task_id)
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return []
    cfg = gamedata.task(task_id) or {}
    slot = bag.get(cfg.get("TaskType"))
    if not isinstance(slot, dict):
        return []
    item = slot.get(task_id)
    if not isinstance(item, dict):
        return []
    cur = int(item.get("state") or 0)
    if cur == TASK_STATE_WAIT_DONE:
        return []
    if not can_transition(cur, TASK_STATE_WAIT_DONE):
        return []
    item["state"] = TASK_STATE_WAIT_DONE
    st.set("task_bag", bag)
    return [_sync(slot.get("__id__"), OP_CHANGE, task_id, item)]


def claim_task(st, task_id, now=None):
    """WaitDone(4) -> Finished(6) 并出包。返回同步描述。

    ★ 只接受 WaitDone 状态 —— 客户端也只会在 WaitDone 时发领奖 RPC
      （node_challenge_task_itemgroup.lua:156）。
    """
    task_id = int(task_id)
    if task_state(st.props, task_id) != TASK_STATE_WAIT_DONE:
        return []
    return finish_task(st, task_id, now)


# ---------------------------------------------------------------- N-d 每日重置

# 跨天标记 / 当日刷新次数（_oo_ 前缀：不同步给客户端，但会随存档持久化）
DAILY_RESET_KEY = "_oo_daily_reset_day"
DAILY_REFRESH_KEY = "_oo_daily_refresh"

# 每天要重置的 TaskType：DailyCycle(3) 每日任务 / TitleDailyRandom(9) 头衔日常
DAILY_RESET_TYPES = (3, 9)


def _day_of(now):
    """把秒级时间戳换算成「天序号」（UTC）。"""
    return int(int(now) // 86400)


def daily_reset(st, now=None):
    """跨天重置：清空每日任务类型的进行中与已完成记录，然后重新播种。

    返回 (重置的类型列表, 同步描述)。

    ★ 为什么需要
      `DailyCycle(3)` 和 `TitleDailyRandom(9)` 语义上是「每天一次」。
      不做重置的话：做完一次就永远完成，第二天也不会再出现。

    ★ 只在「天序号变了」时才动，所以同一天多次登录是幂等的。
      调用点在登录流程（initdict）的快照构建之前，所以重置结果直接进初始快照。
    """
    now = int(now if now is not None else time.time())
    day = _day_of(now)
    if st.props.get(DAILY_RESET_KEY) == day:
        return [], []
    st.props[DAILY_RESET_KEY] = day
    st.props.pop(DAILY_REFRESH_KEY, None)
    reset_types = []
    bag = st.props.get("task_bag")
    done = st.props.get("done_tasks")
    for tt in DAILY_RESET_TYPES:
        touched = False
        if isinstance(bag, dict):
            slot = bag.get(tt)
            if isinstance(slot, dict):
                for k in [k for k in slot if isinstance(k, int)]:
                    slot.pop(k, None)
                    touched = True
                if touched:
                    st.set("task_bag", bag)
        if isinstance(done, dict):
            dslot = done.get(tt)
            if isinstance(dslot, dict):
                for k in [k for k in dslot if isinstance(k, int)]:
                    dslot.pop(k, None)
                    touched = True
                if touched:
                    st.set("done_tasks", done)
        reset_types.append(tt)
    ch = seed_tasks(st, now)
    if reset_types:
        print("[task] 跨天重置 %s，重播 %d 条同步描述"
              % (list(reset_types), len(ch)), flush=True)
    return reset_types, ch


def can_refresh_daily(st, now=None):
    """今天还能不能刷新头衔日常（每天 1 次）。"""
    now = int(now if now is not None else time.time())
    rec = st.props.get(DAILY_REFRESH_KEY)
    if not isinstance(rec, dict):
        return True
    if int(rec.get("day") or -1) != _day_of(now):
        return True
    return int(rec.get("count") or 0) < 1


def mark_daily_refreshed(st, now=None):
    """记一次当日刷新。"""
    now = int(now if now is not None else time.time())
    day = _day_of(now)
    rec = st.props.get(DAILY_REFRESH_KEY)
    if not isinstance(rec, dict) or int(rec.get("day") or -1) != day:
        rec = {"day": day, "count": 0}
    rec["count"] = int(rec.get("count") or 0) + 1
    st.props[DAILY_REFRESH_KEY] = rec
