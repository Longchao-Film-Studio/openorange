
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.story —— 剧情任务状态机（服务端权威）。

数据依据
--------
客户端 entities/avatarcomponent/avatarstorytaskcomp.lua：

    function AvatarStoryTaskComp:getStoryTaskState(taskID)
      local taskServerData = self.story_task_bag[taskID]
      if not taskServerData and self.done_story_tasks[taskID] then
        return GlobalConst.TaskState.Finished      -- 6
      end
      if taskServerData then
        return taskServerData.state
      end
      return -1
    end

    function AvatarStoryTaskComp:getTaskIdByParamInt(param_int, type)
      -- 遍历 story_task_bag，找 TaskType == type 且 Parameter1 含 param_int 的任务
    end

所以服务端要维护两张表：

  story_task_bag[taskId]  = StoryTaskItem{id,state,get_time,start_time,
                                          doing_targets,finish_targets,finish_parameter}
                            —— 「正在做」的任务
  done_story_tasks[taskId] = StoryTaskItem{...}
                            —— 「已完成」的任务

推进流程
--------
  1. 把当前任务放进 story_task_bag，state = Doing(3)
  2. 街景 NPC 的 preTask 命中 Doing(3) -> NPC 出现
  3. 玩家交互 -> RequestDoneStoryTaskParamInt_CS{taskId, param_int}
  4. 服务端：state = Finished(6)、搬进 done_story_tasks、
     按 StoryTaskData[taskId].PostTasks 把下一个任务设为 Doing、
     发 AwardItemList、按 AllSystemCatalogueData 重算系统解锁
  5. 推属性同步

任务状态（common/globalconst.lua:77）
    Idle=1 Init=2 Doing=3 WaitDone=4 Done=5 Finished=6
"""
from . import gamedata

TASK_STATE_IDLE = 1
TASK_STATE_INIT = 2
TASK_STATE_DOING = 3
TASK_STATE_WAIT_DONE = 4
TASK_STATE_DONE = 5
TASK_STATE_FINISHED = 6

# 自定义类型形状（generated/config.lua:6698 StoryTaskItem / :186 ActiveTask）
_STORY_TASK_ITEM = {
    "id": 0, "state": 0, "get_time": 0, "start_time": 0,
    "doing_targets": {}, "finish_targets": {}, "finish_parameter": {},
}
_ACTIVE_TASK = {
    "id": 0, "type": 0, "state": 0, "get_time": 0,
    "complete_count": 0, "cache": {},
}


def _next_id(st):
    return st.next_property_id()


def make_story_task_item(st, task_id, state, now):
    item = {"__tp__": "StoryTaskItem", "__id__": _next_id(st)}
    item.update(_STORY_TASK_ITEM)
    # ★ 嵌套的自定义属性（IntDictKeyInt / IntList）**必须带 __id__**，
    #   但不带 __tp__（见 puppetpropertycomponent.lua:161-175：
    #   _changeRootProperty 只对外层做 __tp__ 校验，嵌套对象靠 __id__ 定位）。
    #   漏了 __id__ 客户端会报
    #     class ClientAvatar function init must have a boolean return value
    #   实测过（2026-10-20 阶段 2 实机验证）。
    item["doing_targets"] = {"__id__": _next_id(st)}
    item["finish_targets"] = {"__id__": _next_id(st)}
    item["finish_parameter"] = {"__id__": _next_id(st)}
    item["id"] = int(task_id)
    item["state"] = int(state)
    item["get_time"] = int(now)
    item["start_time"] = int(now)
    return item


def make_active_task(st, task_id, task_type, state, now):
    item = {"__tp__": "ActiveTask", "__id__": _next_id(st)}
    item.update(_ACTIVE_TASK)
    item["id"] = int(task_id)
    item["type"] = int(task_type)
    item["state"] = int(state)
    item["get_time"] = int(now)
    return item


def _as_map(props, name, tp=None):
    """取一个 dict 属性；不存在就建一个（带 __tp__/__id__ 需要调用方补）。"""
    v = props.get(name)
    if isinstance(v, dict):
        return v
    return None


# 主线第一环：StoryTaskData[1000]「好戏开拍」（main_01 笼律鸟）
FIRST_STORY_TASK = 1000


def init_new_player(st, now=None):
    """新号的剧情起点：把第一环设为进行中。

    这是**正式路径**，不是脚手架：真实玩家进游戏时，服务端本来就该
    把第一个剧情任务放进 story_task_bag 并设为 Doing(3)。
    街景 NPC 的 preTask 命中 Doing(3) 才会出现，所以这一步同时
    决定了「街上有没有 NPC」。

    返回启动的任务 id 列表。
    """
    import time as _t
    now = int(now if now is not None else _t.time())
    out = []
    changes = start_story_task(st, FIRST_STORY_TASK, now)
    if changes:
        out.append(FIRST_STORY_TASK)
        print("[story] 新号剧情起点：任务 %s 设为进行中" % FIRST_STORY_TASK, flush=True)
    return out


def story_state(props, task_id):
    bag = _as_map(props, "story_task_bag")
    if isinstance(bag, dict):
        it = bag.get(task_id)
        if isinstance(it, dict):
            return int(it.get("state") or 0)
    done = _as_map(props, "done_story_tasks")
    if isinstance(done, dict) and isinstance(done.get(task_id), dict):
        return TASK_STATE_FINISHED
    return -1


def start_story_task(st, task_id, now):
    """把一个剧情任务设为「进行中」。已在进行/已完成则不动。"""
    props = st.props
    bag = props.get("story_task_bag")
    if not isinstance(bag, dict):
        return []
    if story_state(props, task_id) in (TASK_STATE_DOING, TASK_STATE_FINISHED):
        return []
    item = make_story_task_item(st, task_id, TASK_STATE_DOING, now)
    bag[task_id] = item
    st.set("story_task_bag", bag)
    return [("add", "story_task_bag", task_id, item)]


def finish_story_task(st, task_id, now):
    """标记剧情任务完成，返回要推的同步列表。

    同步项是 ("root", 属性名, 值) 或 ("sub", 容器属性名, 键, 值)。
    """
    props = st.props
    out = []
    bag = props.get("story_task_bag")
    done = props.get("done_story_tasks")
    if not isinstance(bag, dict) or not isinstance(done, dict):
        return out

    item = make_story_task_item(st, task_id, TASK_STATE_FINISHED, now)
    done[task_id] = item
    bag.pop(task_id, None)
    st.set("done_story_tasks", done)
    st.set("story_task_bag", bag)
    out.append(("add", "done_story_tasks", task_id, item))
    out.append(("del", "story_task_bag", task_id, None))

    # 按 PostTasks 推进下一个
    cfg = gamedata.story_task(task_id) or {}
    posts = cfg.get("PostTasks") or []
    for nxt in posts:
        out.extend(start_story_task(st, int(nxt), now))
    return out


def story_award(st, task_id):
    """发 StoryTaskData[task_id].AwardItemList 的奖励，返回 (item_id, 新数量) 列表。"""
    cfg = gamedata.story_task(task_id) or {}
    awards = cfg.get("AwardItemList") or {}
    out = []
    for k, v in awards.items():
        if k in ("KeyList", "__bdd_metatype__"):
            continue
        try:
            item_id = int(k)
            count = int(v)
        except Exception:
            continue
        if count <= 0:
            continue
        got = add_item(st.props, item_id, count)
        if got is not None:
            newnum, grid_key, is_new = got
            out.append((item_id, newnum, grid_key, is_new))
    return out


# ---------------------------------------------------------------------------
# 剧情触发引擎（TaskType 3 / 6 / 7）
# ---------------------------------------------------------------------------
# ★ 逆向结论（2026-10-20，查全 BDD 与 Lua 后确认）：
#
# 客户端 `avatarstorytaskcomp.lua:667-681` 的 RequestDoneStoryTaskParamInt_CS(type, param_int)
# 只是**本地筛出 taskId** 然后发 {taskId, param_int} —— 所以 type 1/2/5 的完成是客户端驱动的。
#
# 但 TaskType 3/7 的 81+78 条任务，客户端**全项目没有任何调用点**会发它们。
# 顺着 Parameter1 查下去才找到真正的机制：
#
#   StoryTask 1000 (TaskType=3, Parameter1=[1])
#     -> TaskData[1] = {Description:"完成序章", TriggerEventList:[4001]}
#     -> TaskEventData[4001] = {EventType:"FinishDungeon",
#                              EventParam:{typeId:1004, modeId:1, level:1}}
#
# 也就是说：**TaskType 3/7 的完成条件是「服务端监听游戏事件」**，
# 事件命中 TaskEventData 就把对应剧情任务标完成。
#
# TaskType 分布（StoryTaskData 1047 条）：
#   0 -> 270  无完成条件（纯叙述）
#   1 -> 380  客户端发 (1, storyId)   —— 剧情演出/对话/timeline
#   2 -> 199  客户端发 (2, actorId)   —— 街景 NPC 交互
#   3 ->  81  Parameter1 = TaskData id，服务端事件判定
#   4 ->  13  Parameter1 = 街区 id
#   5 ->   2  客户端发 (5, videoId)   —— 视频播完
#   6 ->  24  Parameter1 = 前置剧情任务 id 列表，全部完成则本任务完成
#   7 ->  78  Parameter1 = TaskData id，服务端事件判定（同 3）

TASK_TYPE_CLIENT = (1, 2, 5)   # 客户端驱动
TASK_TYPE_EVENT = (3, 7)       # 服务端按 TaskData -> TaskEventData 判定
TASK_TYPE_CHAIN = (6,)         # 前置剧情任务全完成
TASK_TYPE_STREET = (4,)        # 街区 id（暂未接）


def _active_story_tasks(st):
    """返回 [(taskId, StoryTaskItem)]，只含进行中的。"""
    bag = _as_map(st.props, "story_task_bag")
    if not isinstance(bag, dict):
        return []
    return [(k, v) for k, v in bag.items()
            if isinstance(k, int) and isinstance(v, dict)]


def event_matches(event_cfg, event_type, params):
    """TaskEventData 是否命中本次事件。

    EventParam 里除 KeyList/__bdd_metatype__ 外的每个键都必须出现在 params 里且相等。
    例：EventParam={typeId:1004, modeId:1, level:1}，
        则 params 必须同时给出这三个值。
    """
    if not isinstance(event_cfg, dict):
        return False
    if event_cfg.get("EventType") != event_type:
        return False
    ep = event_cfg.get("EventParam") or {}
    if not isinstance(ep, dict):
        return True
    for k, v in ep.items():
        if k in ("KeyList", "__bdd_metatype__"):
            continue
        if k not in params:
            return False
        try:
            if int(params[k]) != int(v):
                return False
        except Exception:
            if params[k] != v:
                return False
    return True


def evaluate_event(st, event_type, params, now=None):
    """按游戏事件推进剧情任务（TaskType 3/7），返回 (完成的taskId列表, 同步列表)。

    params 例：
      FinishDungeon -> {"typeId": 1004, "modeId": 1, "level": 1}
      CharData      -> {"level": 10}
    """
    import time as _t
    now = int(now if now is not None else _t.time())
    hits = []
    for task_id, _item in _active_story_tasks(st):
        cfg = gamedata.story_task(task_id) or {}
        if cfg.get("TaskType") not in TASK_TYPE_EVENT:
            continue
        p1 = cfg.get("Parameter1") or []
        if not isinstance(p1, list):
            continue
        done = False
        for pid in p1:
            td = gamedata.task_data(pid)
            if not isinstance(td, dict):
                continue
            for eid in (td.get("TriggerEventList") or []):
                if event_matches(gamedata.task_event(eid), event_type, params):
                    done = True
                    break
            if done:
                break
        if done:
            hits.append(task_id)
    # ★ 只做检测，不落状态 —— 落状态统一走 entity_rpc._finish_story_tasks，
    #   那里才发奖 + 推同步 + 重算解锁。
    #   踩过的坑：这里如果直接 finish_story_task，外层再调 _finish_story_tasks 时
    #   会因为已经是 Finished 而跳过，导致**不发奖、不推同步帧**。
    if hits:
        print("[story] 事件 %s%s 命中剧情任务 %s" % (event_type, params, hits),
              flush=True)
    return hits, []


def check_chain_tasks(st, now=None):
    """TaskType=6：Parameter1 里的前置剧情任务全部完成 -> 本任务完成。

    例：StoryTask 201000「完成第一章」Parameter1=[1031]，1031 完成即本任务完成。
    """
    import time as _t
    now = int(now if now is not None else _t.time())
    hits = []
    for task_id, _item in _active_story_tasks(st):
        cfg = gamedata.story_task(task_id) or {}
        if cfg.get("TaskType") not in TASK_TYPE_CHAIN:
            continue
        p1 = cfg.get("Parameter1") or []
        if not isinstance(p1, list) or not p1:
            continue
        if all(story_state(st.props, int(x)) == TASK_STATE_FINISHED for x in p1):
            hits.append(task_id)
    # 同 evaluate_event：只检测，不落状态。
    if hits:
        print("[story] 前置任务全完成，命中 %s" % hits, flush=True)
    return hits, []

def add_item(props, item_id, count):
    """往 item_bag 加道具（有则叠加，无则新建格子）。

    返回 (新数量, 格子键, 是否新建)。是否新建决定同步用 OP_ADD 还是 OP_CHANGE。
    """
    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        return None
    for key, grid in bag.items():
        if not isinstance(grid, dict) or grid.get("itemId") != item_id:
            continue
        old = int(grid.get("num") or 0)
        grid["num"] = old + int(count)
        return grid["num"], key, False
    # 新建格子：找一个没用的整数键
    used = {k for k in bag if isinstance(k, int)}
    new_key = (max(used) + 1) if used else 1
    while new_key in used:
        new_key += 1
    bag[new_key] = {
        "__id__": _next_id_for_props(props),
        "id": str(new_key),
        "itemId": int(item_id),
        "num": int(count),
        "expireTime": 0.0,
        "getTime": 0.0,
    }
    return int(count), new_key, True


def _next_id_for_props(props):
    """给嵌套对象分配 __id__（不走 PlayerState 时用）。"""
    mx = _max_id(props)
    return mx + 1


def _max_id(obj, cur=0):
    if isinstance(obj, dict):
        v = obj.get("__id__")
        if isinstance(v, int) and v > cur:
            cur = v
        for v2 in obj.values():
            cur = _max_id(v2, cur)
    elif isinstance(obj, list):
        for v in obj:
            cur = _max_id(v, cur)
    return cur
