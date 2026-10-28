
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

from shared.log_tee import get_logger

from . import gamedata
from .story.state_machine import (
    TASK_STATE_DOING,
    TASK_STATE_FINISHED,
    TASK_STATE_INIT,
    TASK_STATE_WAIT_DONE,
    can_transition,
)
from .write_guard import write_guard

_log = get_logger(__name__)

# 同步描述里的 optype（和客户端 puppetpropertycomponent.lua 一致）
# 由本模块自己决定，不让上层猜 —— 审计发现上层一律用 OP_CHANGE，
# 新建的 task_bag[type] 槽位 / 任务项都发错了。
OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3

# 事件命中账本：只落盘、不同步给客户端（_oo_ 前缀约定）。
# 用途：事件发生时任务可能还不在包里（PreTask 还没完成），账本记住"发生过什么"，
# 任务播种后补算（H4：任务 1「完成序章」就是这种情况）。
EVENT_HITS_KEY = "_oo_task_event_hits"
EVENT_HITS_MAX = 4096


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
        if cur not in (TASK_STATE_DOING, TASK_STATE_FINISHED) and can_transition(
                cur, TASK_STATE_DOING):
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

def all_task_ids():
    """TaskData 全部 uiD（升序，保证可复现）。"""
    d = gamedata.table("TaskData") or {}
    out = []
    for v in (d.values() if isinstance(d, dict) else d):
        if isinstance(v, dict) and v.get("uiD") is not None:
            try:
                out.append(int(v["uiD"]))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
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


def _seed_once(st, now, limit=400):
    """播种一轮（前置满足 + 不在包 + 未完成）。返回同步描述。"""
    out = []
    seeded = 0
    cands = []
    for tid in all_task_ids():
        if task_state(st.props, tid) != -1:
            continue
        if not pre_satisfied(st, tid):
            continue
        cands.append(tid)
    for tid in cands:
        if seeded >= limit:
            _log.info(f'[task] 播种达到上限 {limit}，还剩 {len(cands) - seeded} 个候选未播')
            break
        ch = start_task(st, tid, now)
        if ch:
            out.extend(ch)
            seeded += 1
    if seeded:
        _log.info(f'[task] 通用任务播种：{seeded} 个进包（候选 {len(cands)} 个）')
    return out


def seed_tasks(st, now=None, limit=400):
    """把「前置满足 + 不在包 + 未完成」的通用任务播进 task_bag，并做事件补算。

    返回同步描述列表（格式同 start_task，交给 story/tasks.py:_task_sync_frames 编帧）。

    ★ 为什么必须播种（P1 阶段 N-a2）
      之前 task_bag 实测 0 槽位、0 任务 —— 通用任务一条都不存在，于是：
        getSystemTaskState(任意) 恒 -1
        -> AllSystemCatalogueData 里 40 条 unlockType=1 的系统入口数据条件永假
      见 docs/P1-剧情任务状态机.md 阶段 N 的 N2.1 ④。

    ★ 2026-10-27（H4）：播种后补算历史事件。任务可能因为 PreTask 而「晚生」，
      但它的触发事件早就发生过（任务 1「完成序章」就是这样），不补算永远完不成。
      补算完成的任务可能又让别的任务 PreTask 满足 -> 最多跑 3 轮（数据无环）。
    """
    now = int(now if now is not None else time.time())
    out = []
    for _round in range(3):
        round_ch = _seed_once(st, now, limit)
        out.extend(round_ch)
        cu = catch_up_task_events(st, now)
        out.extend(cu)
        if not round_ch and not cu:
            break
    return out


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


# ---------------------------------------------------------------- 事件账本 / 补算（H4）

def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _events_rows(event_type):
    """某个 EventType 下的 [(uiD, row)]（空列表表示数据表里没有这种事件）。"""
    return gamedata.task_events_by_type().get(str(event_type or ""), [])


def _event_hits(props):
    raw = props.get(EVENT_HITS_KEY)
    return raw if isinstance(raw, dict) else {}


def record_event_hits(st, event_type, params):
    """把本次事件命中的 TaskEventData 记进账本；返回命中条数。

    ★ 记录时机：只记**未被去重**的事件（调用方在 apply_event 的去重之后调）。
      - 有 event_id 的事件（Sign/FinishTask/StreetInteract…）由 _oo_event_seen 去重；
      - FinishDungeon 没有 event_id（每次结算都是真事件），所以每次都会累计 ——
        这正是 TriggerTimes 多档任务需要的语义。
    """
    from .story import event_matches

    rows = _events_rows(event_type)
    if not rows:
        return 0
    hits = _event_hits(st.props)
    n = 0
    for eid, row in rows:
        if not event_matches(row, event_type, params):
            continue
        key = str(eid)
        hits[key] = int(hits.get(key) or 0) + 1
        n += 1
    if not n:
        return 0
    if len(hits) > EVENT_HITS_MAX:
        # 只可能是数据表被替换后异常膨胀：保留后面（较新）的部分
        _log.warning("[task] 事件账本超过上限 %s，截断", EVENT_HITS_MAX)
        hits = dict(list(hits.items())[-EVENT_HITS_MAX:])
    st.set(EVENT_HITS_KEY, hits)
    return n


def _derive_sign(props, out):
    """Sign：有名字就说明完成过取名。"""
    if props.get("player_name"):
        for eid, _row in _events_rows("Sign"):
            out[str(eid)] = 1


def _derive_finish_dungeon(props, out):
    """FinishDungeon：从已完成的剧情任务（Type 3/7）反推它满足过的 TaskData 事件。

    只在 TaskData.TriggerEventList 恰好 1 个时推导 —— 多事件是 OR 语义，
    推导会过度完成（宁可漏补，不可错补）。
    """
    done_story = props.get("done_story_tasks")
    if not isinstance(done_story, dict):
        return
    for k in list(done_story):
        if str(k).startswith("__"):
            continue
        sid = _as_int(k)
        if sid is None:
            continue
        srow = gamedata.story_task(sid) or {}
        if _as_int(srow.get("TaskType")) not in (3, 7):
            continue
        for tdid in (srow.get("Parameter1") or []):
            td = gamedata.task_data(tdid) or {}
            evs = td.get("TriggerEventList") or []
            if len(evs) != 1:
                continue
            eid = _as_int(evs[0])
            if eid is not None:
                out[str(eid)] = 1


def _derive_get_char(props, out):
    """GetChar：角色包里有的角色，说明它的 GetChar 事件发生过。"""
    cb = props.get("characterBag")
    if not isinstance(cb, dict):
        return
    chars = {c for c in (_as_int(k) for k in cb if not str(k).startswith("__")) if c}
    if not chars:
        return
    for eid, row in _events_rows("GetChar"):
        cid = _as_int((row.get("EventParam") or {}).get("charId"))
        if cid is not None and cid in chars:
            out[str(eid)] = 1


def _done_task_ids(props):
    """已完成的任务 id 集合（通用任务的 done_tasks + 剧情任务的 done_story_tasks）。"""
    ids = set()
    done = props.get("done_tasks")
    if isinstance(done, dict):
        for slot in done.values():
            if not isinstance(slot, dict):
                continue
            for k in slot:
                tid = _as_int(k)
                if tid is not None:
                    ids.add(tid)
    done_story = props.get("done_story_tasks")
    if isinstance(done_story, dict):
        for k in done_story:
            sid = _as_int(k)
            if sid is not None:
                ids.add(sid)
    return ids


def _derive_finish_task(props, out):
    """FinishTask：已完成的任务对应的 FinishTask 事件。"""
    done_ids = _done_task_ids(props)
    if not done_ids:
        return
    for eid, row in _events_rows("FinishTask"):
        tid = _as_int((row.get("EventParam") or {}).get("taskId"))
        if tid is not None and tid in done_ids:
            out[str(eid)] = 1


def derivable_event_hits(st):
    """从**当前状态**能确定推导出的历史事件 {eventId(str): 1}。

    只做有确定来源的 4 类（其余靠账本，补不回的写进文档）：
      · Sign / FinishDungeon / GetChar / FinishTask
    """
    out = {}
    props = st.props
    for fn in (_derive_sign, _derive_finish_dungeon, _derive_get_char, _derive_finish_task):
        fn(props, out)
    return out


def catch_up_task_events(st, now=None):
    """按「账本 ∪ 状态推导」补算通用任务进度；返回同步描述（幂等，不重复完成）。"""
    now = int(now if now is not None else time.time())
    hits = {}
    for src in (_event_hits(st.props), derivable_event_hits(st)):
        for k, v in src.items():
            hits[k] = max(int(hits.get(k) or 0), _as_int(v) or 0)
    if not hits:
        return []
    bag = st.props.get("task_bag")
    if not isinstance(bag, dict):
        return []

    out = []
    touched = False
    for tt, slot in list(bag.items()):
        if not isinstance(tt, int) or not isinstance(slot, dict):
            continue
        for tid, item in list(slot.items()):
            if not isinstance(tid, int) or not isinstance(item, dict):
                continue
            if int(item.get("state") or 0) != TASK_STATE_DOING:
                continue
            cfg = gamedata.task(tid) or {}
            evs = [_as_int(e) for e in (cfg.get("TriggerEventList") or [])]
            evs = [e for e in evs if e is not None]
            if not evs:
                continue
            need = _as_int((cfg.get("TriggerTimes") or [1])[0]) or 1
            dt = item.get("doing_targets")
            if not isinstance(dt, dict):
                dt = {"__id__": st.next_property_id()}
                item["doing_targets"] = dt
            changed = False
            for eid in evs:
                have = int(hits.get(str(eid)) or 0)
                if have <= 0:
                    continue
                target = min(have, need)
                if target > int(dt.get(eid) or 0):
                    dt[eid] = target
                    changed = True
            if not changed:
                continue
            touched = True
            total = sum(int(v) for k, v in dt.items() if isinstance(k, int))
            out.append(_sync(slot.get("__id__"), OP_CHANGE, tid, item))
            if total >= need:
                out.extend(finish_task(st, tid, now))
    if touched:
        st.set("task_bag", st.props.get("task_bag"))
    if out:
        _log.info("[task] 事件补算：%s 条同步描述（账本 %s 条 / 状态推导 %s 条）",
                  len(out), len(_event_hits(st.props)), len(derivable_event_hits(st)))
    return out


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


def move_to_wait_done(st, task_id, _now=None):
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


def day_of(now):
    """把秒级时间戳换算成「天序号」（UTC）。"""
    return int(int(now) // 86400)


def daily_reset(st, now=None):
    #属性写入口，在这里开守卫（跨天重置）。
    with write_guard("daily_reset"):
        """跨天重置：清空每日任务类型的进行中与已完成记录，然后重新播种。

        返回 (重置的类型列表, 同步描述)。

        ★ 为什么需要
          `DailyCycle(3)` 和 `TitleDailyRandom(9)` 语义上是「每天一次」。
          不做重置的话：做完一次就永远完成，第二天也不会再出现。

        ★ 只在「天序号变了」时才动，所以同一天多次登录是幂等的。
          调用点在登录流程（initdict）的快照构建之前，所以重置结果直接进初始快照。
        """
        now = int(now if now is not None else time.time())
        day = day_of(now)
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
            _log.info(f'[task] 跨天重置 {list(reset_types)}，重播 {len(ch)} 条同步描述')
        return reset_types, ch


def can_refresh_daily(st, now=None):
    """今天还能不能刷新头衔日常（每天 1 次）。"""
    now = int(now if now is not None else time.time())
    rec = st.props.get(DAILY_REFRESH_KEY)
    if not isinstance(rec, dict):
        return True
    if int(rec.get("day") or -1) != day_of(now):
        return True
    return int(rec.get("count") or 0) < 1


def mark_daily_refreshed(st, now=None):
    #属性写入口，在这里开守卫（跨天刷新标记）。
    with write_guard("mark_daily_refreshed"):
        """记一次当日刷新。"""
        now = int(now if now is not None else time.time())
        day = day_of(now)
        rec = st.props.get(DAILY_REFRESH_KEY)
        if not isinstance(rec, dict) or int(rec.get("day") or -1) != day:
            rec = {"day": day, "count": 0}
        rec["count"] = int(rec.get("count") or 0) + 1
        st.props[DAILY_REFRESH_KEY] = rec
