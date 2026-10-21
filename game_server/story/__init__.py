
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
from .. import gamedata
from ..property import ids as _ids

from .state_machine import (  # noqa: F401  (re-export：业务模块继续用 story.TASK_STATE_*)
    TASK_STATE_CLOSED, TASK_STATE_DOING, TASK_STATE_DONE, TASK_STATE_FAILED,
    TASK_STATE_FINISHED, TASK_STATE_IDLE, TASK_STATE_INIT, TASK_STATE_WAIT_DONE,
    TransitionError, can_transition, make_story_task_item, state_name,
)
from .state_machine import create_task as _sm_create_task
from .state_machine import current_state as _sm_current_state
from .state_machine import move_to_done as _sm_move_to_done
from .state_machine import transition as _sm_transition

# 自定义类型形状（generated/config.lua:186 ActiveTask）
_ACTIVE_TASK = {
    "id": 0, "type": 0, "state": 0, "get_time": 0,
    "complete_count": 0, "cache": {},
}


def _next_id(st):
    return st.next_property_id()


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

# 发奖账本："taskId:itemId" -> 已发数量。
# ★ 为什么需要它（审计 P0-2）：story_award 以前直接累加 num，同任务调两次奖励翻倍。
#   而「标记完成 -> 发奖 -> 归档」拆成三步之后中间失败会重试，所以发奖必须幂等。
_AWARD_LOG = "_oo_story_award_log"


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
    """任务状态；-1 = 不在包里也不在 done。实现在 state_machine.current_state。"""
    return _sm_current_state(props, task_id)


def start_story_task(st, task_id, now):
    """把一个剧情任务设为「进行中」，返回同步描述。

    ★ 为什么是两条而不是一条（P1 阶段 A）：
      客户端 onStoryTaskItemChanged 只对 OP_CHANGE 触发，而 OP_ADD 走的是
      onStoryTaskBagInserted —— 那是个空函数（avatarstorytaskcomp.lua:557-558），
      所以「只发 OP_ADD」等于界面上什么都没发生。
      正确做法是：OP_ADD 建出 Init(2) 条目 -> OP_CHANGE 迁到 Doing(3)，
      这样客户端拿到的 old.state=Init、new.state=Doing，命中
      avatarstorytaskcomp.lua:409-434 的「接任务」分支（章节开始提示 + 红点）。
    """
    props = st.props
    if not isinstance(props.get("story_task_bag"), dict):
        return []
    cur = story_state(props, task_id)
    if cur not in (-1, TASK_STATE_INIT):
        return []
    out = _sm_create_task(st, task_id, now)
    out.extend(_sm_transition(st, task_id, TASK_STATE_DOING,
                              source="start_story_task"))
    return out


def mark_finish_parameter(st, task_id, param):
    """把「已满足的一个目标」记进 finish_parameter，返回子属性同步描述。

    ★ 依据（客户端 ui/panel/streetv2panel/streettasktrackcomp.lua:108-134）：
      getStoryTaskDoingTargets 把 finish_parameter 里的值拿去和 Parameter1 逐个比对，
      命中的下标对应的 GuideParam 目标就不再显示追踪箭头 ——
      所以 finish_parameter 是「已完成的 Parameter1 值」列表。

    ★ 只有 Parameter1 多于 1 项的任务才有意义（全表 11 条，其中 8 条带 GuideParam）。
      单目标任务永远不需要它。

    ★ 表示形式：CustomList（spec.py:132 —— list 类型的键是 "#N"，序列化后是 msgpack 数组），
      所以这里是 {"__id__": N, "#1": v1, "#2": v2, ...}。
    """
    task_id = int(task_id)
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        return []
    item = bag.get(task_id)
    if not isinstance(item, dict):
        return []
    fp = item.get("finish_parameter")
    if not isinstance(fp, dict):
        fp = {"__id__": st.next_property_id()}
    for k, v in fp.items():
        if str(k).startswith("#") and int(v) == int(param):
            return []
    used = [int(str(k)[1:]) for k in fp
            if str(k).startswith("#") and str(k)[1:].isdigit()]
    fp["#%d" % ((max(used) + 1) if used else 1)] = int(param)
    item["finish_parameter"] = fp
    st.set("story_task_bag", bag)
    return [("child", item, "finish_parameter", fp)]


def finish_parameters_of(item):
    """读出 finish_parameter 里的值集合。"""
    fp = item.get("finish_parameter") if isinstance(item, dict) else None
    if not isinstance(fp, dict):
        return set()
    out = set()
    for k, v in fp.items():
        if str(k).startswith("#"):
            try:
                out.add(int(v))
            except Exception:
                pass
    return out


def mark_story_task_finished(st, task_id, now):
    """只做「→Finished(6)」这一步，返回同步描述（可能含一条补建的 OP_ADD）。

    ★ 拆开的原因：发奖要插在「标记完成」和「归档进 done」之间。
      如果先归档再发奖，发奖一旦抛异常，任务已经进了 done，奖励就永远丢了；
      现在发奖失败时任务还留在 story_task_bag 里且状态是 Finished，可以重试。

    ★ 任务不在包里（GM 命令 / 旧存档补偿 / 部分测试会这样调）时，
      走 Idle -> Init -> Finished 两条**合法**迁移补建，不跳表；
      两条描述一起返回，否则客户端会收到「改一个不存在的键」的 OP_CHANGE 而报错。
    """
    out = []
    props = st.props
    if not isinstance(props.get("story_task_bag"), dict):
        return out
    cur = story_state(props, task_id)
    if cur == TASK_STATE_FINISHED:
        # 已在包里 Finished（上次发奖失败留下的）或已归档 -> 幂等 no-op
        return out
    if cur == -1:
        out.extend(_sm_create_task(st, task_id, now))
    out.extend(_sm_transition(st, task_id, TASK_STATE_FINISHED,
                              source="finish_story_task"))
    return out


def archive_story_task(st, task_id, now):
    """把已 Finished 的任务归档进 done_story_tasks，并启动 PostTasks 的下一环。

    返回 [("add", "done_story_tasks", ...), ("del", "story_task_bag", ...)] + 下一环的启动描述。
    """
    out = _sm_move_to_done(st, task_id, now)
    if not out:
        # 没归档成功（任务不在包里）就不许推进 PostTasks，否则会「没完成却开了下一环」
        return out
    cfg = gamedata.story_task(task_id) or {}
    for nxt in (cfg.get("PostTasks") or []):
        out.extend(start_story_task(st, int(nxt), now))
    return out


def finish_story_task(st, task_id, now):
    """完成剧情任务（兼容封装）：→Finished + 归档 + 推进 PostTasks。

    ★ 生产路径不要用这个 —— 它会把发奖跳过去。
      _finish_story_tasks_inner 用的是 mark_story_task_finished +
      story_award + archive_story_task 三步，奖励才有地方发。
    """
    out = mark_story_task_finished(st, task_id, now)
    out.extend(archive_story_task(st, task_id, now))
    return out


def story_award(st, task_id, now=None):
    """发 StoryTaskData[task_id].AwardItemList 的奖励，返回 (item_id, 新数量, 格子键, 是否新建)。

    ★ 幂等（审计 P0-2）：每发一项就把 "taskId:itemId" 记进 `_oo_story_award_log`，
      重复调用会跳过已发过的项。这样「标记完成 -> 发奖 -> 归档」中途失败后重试，
      既不会丢奖励，也不会把奖励翻倍。
    """
    cfg = gamedata.story_task(task_id) or {}
    awards = cfg.get("AwardItemList") or {}
    props = st.props
    log = props.get(_AWARD_LOG)
    if not isinstance(log, dict):
        log = {}
        st.set(_AWARD_LOG, log)
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
        key = "%d:%d" % (int(task_id), item_id)
        if key in log:
            continue
        got = add_item(props, item_id, count)
        if got is None:
            continue
        newnum, grid_key, is_new = got
        log[key] = int(count)
        st.set(_AWARD_LOG, log)
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
    # ★ 新建格子用**字符串键**。
    #   依据：ItemBag 的 __IntTypeKey__ = false（config.lua Custom.ItemBag），
    #   客户端 avatarbagcomp.lua:223 是 item_bag[gridId] 字符串查询。
    #   之前用整数键 -> 同步时 str(gkey) 把客户端已有的 "1" 原地覆盖，
    #   发奖把背包第 1 格顶掉；重登后新道具用字符串键查不到，等于消失。
    #   （审计实锤：剧情发奖 / 宝箱 / 副本结算三条路径全部走这里）
    used = {int(k) for k in bag if isinstance(k, int)}
    used |= {int(k[1:]) for k in bag
             if isinstance(k, str) and k.startswith("#") and k[1:].isdigit()}
    used |= {int(k) for k in bag if isinstance(k, str) and k.isdigit()}
    n = (max(used) + 1) if used else 1
    while str(n) in bag:
        n += 1
    new_key = str(n)
    bag[new_key] = {
        "__id__": _ids.alloc_id(props),
        "id": new_key,
        "itemId": int(item_id),
        "num": int(count),
        "expireTime": 0.0,
        "getTime": 0.0,
    }
    return int(count), new_key, True   # new_key 现在是字符串


