#!/usr/bin/env python3
"""game_server.story.seed —— 剧情任务播种器（阶段 C）。

★ 为什么需要它

  以前只播 FIRST_STORY_TASK(1000) 一个根，靠 finish_story_task 的 PostTasks 往下走，
  实测只能到达 140/1047 条任务。而 StoryTaskData 里有 **83 个无 PreTask 的根**
  （23 个 type6 章节闸门 / 22 个 type1 / 21 个 type3 / 13 个 type2 / 3 个 type0 / 1 个 type4），
  从全部根 BFS 可达 **1032/1047**。

  根任务没有任何上游 PostTasks 指向它，所以「不播种就永远不进包」——
  这就是主线第 2 章以后永远不开的直接原因（章节闸门死锁）。

★ 不变量

  1. 播种只产生 OP_ADD(Init) + OP_CHANGE(Doing) 两条帧（走 start_story_task，不另拼帧）。
  2. 已在 story_task_bag 里或 done_story_tasks 里的任务绝不重复播种。
  3. 一次播种有上限（防任务图成环无限播种），超限记日志并停。
  4. 播种条件只有两个：PreTask 已满足 + 不在包/不在 done。
     不按 TaskChapterData.StartTask 播种（103 个里只有 7 个是根，
     4 个根本不在任务表里，按章节表播会重复启动链中间节点）。

★ 依据

  - 83 根 / 22 分叉 / 153 终点 / 0 汇合（958 条边入度全为 1）：本次全表扫描实测。
  - PreTaskType=2 -> PreTask 指向 StoryTaskData（958 条）；
    PreTaskType=1 -> PreTask 指向 TaskData（5 条，跨表前置，见 seed_cross_table）。
  - LevelOn(12 条)/MissionOn(6 条) 在 83 个根里一条都没有，所以播种期不需要过滤；
    客户端 checkTaskIsEnabled（avatarstorytaskcomp.lua:769-806）只影响显示/红点，不阻断完成。
  - activityOn(94 条，其中 3 条是根)：isActivityValid 需要服务端推送的 cur_activity_info
    （avataractivitycomp.lua:437-462 state==OPEN），我们没实现活动系统 -> 无法判定。
    按文档既定策略：放行 + 打日志（不猜「活动没开」）。
"""
import time

from .. import gamedata
from .. import story as story_mod
from .state_machine import TASK_STATE_FINISHED

from shared.log_tee import get_logger
_log = get_logger(__name__)

# 一次播种的硬上限：任务图理论上无环，但数据不一定可靠。
SEED_LIMIT = 200

_INDEX = None          # {taskId: cfg}
_ROOTS = None          # [taskId, ...]
_CHILDREN = None       # {preTaskId: [taskId, ...]}  反向边


def _build_index(force=False):
    """建一次全表索引（进程内缓存）。"""
    global _INDEX, _ROOTS, _CHILDREN
    # ★ 三个缓存要一起判断：只判 _INDEX 的话，外部把 _ROOTS 清成 None 后
    #   会因为 _INDEX 还在而直接返回，留下 _ROOTS=None 的坏状态。
    if _INDEX is not None and _ROOTS is not None and not force:
        return _INDEX
    table = gamedata.table("StoryTaskData") or {}
    idx = {}
    items = table.items() if isinstance(table, dict) else enumerate(table)
    for k, v in items:
        if not isinstance(v, dict):
            continue
        tid = v.get("TaskID")
        if tid is None:
            try:
                tid = int(k)
            except Exception:
                continue
        idx[int(tid)] = v
    _INDEX = idx
    _ROOTS = sorted(t for t, v in idx.items() if not v.get("PreTask"))
    _CHILDREN = {}
    for t, v in idx.items():
        pre = v.get("PreTask")
        if pre is None:
            continue
        try:
            _CHILDREN.setdefault(int(pre), []).append(t)
        except Exception:
            continue
    return _INDEX


def roots():
    """全部无 PreTask 的根任务 id（83 条）。"""
    _build_index()
    return list(_ROOTS or [])


def children_of(task_id):
    """反向边：PreTask 指向 task_id 的任务（用于增量播种）。"""
    _build_index()
    return list((_CHILDREN or {}).get(int(task_id)) or [])


def cfg_of(task_id):
    _build_index()
    return (_INDEX or {}).get(int(task_id))


def pre_satisfied(st, task_id):
    """PreTask 条件是否满足。

    PreTaskType=2 -> PreTask 指向 StoryTaskData（用 story_state 判 Finished）。
    PreTaskType=1 -> PreTask 指向 TaskData（通用任务，用 tasks.task_state 判）。
    """
    cfg = cfg_of(task_id) or {}
    pre = cfg.get("PreTask")
    if pre is None:
        return True
    try:
        pre = int(pre)
    except Exception:
        return False
    if cfg.get("PreTaskType") == 1:
        # 跨表前置：PreTask 指向 TaskData（全表只有 5 条）
        from .. import tasks as _tasks
        return _tasks.task_state(st.props, pre) == TASK_STATE_FINISHED
    return story_mod.story_state(st.props, pre) == TASK_STATE_FINISHED


def candidate_tasks(st, only_children_of=None):
    """返回「该进包但还没进包」的任务 id 列表（已排序，保证可复现）。

    only_children_of：只考虑 PreTask 指向这个任务的（增量播种用）。
    """
    _build_index()
    bag = st.props.get("story_task_bag")
    bag = bag if isinstance(bag, dict) else {}
    done = st.props.get("done_story_tasks")
    done = done if isinstance(done, dict) else {}
    pool = list(_ROOTS or []) if only_children_of is None \
        else children_of(only_children_of)
    out = []
    for tid in pool:
        if tid in bag or tid in done:
            continue
        if not pre_satisfied(st, tid):
            continue
        out.append(tid)
    out.sort()
    return out


def seed(st, now=None, only_children_of=None, limit=SEED_LIMIT):
    """播种：把候选任务放进 story_task_bag，返回同步描述列表。

    只走 start_story_task（OP_ADD Init + OP_CHANGE Doing），不另拼帧。
    超上限记日志并停（防任务图成环）。
    """
    now = int(now if now is not None else time.time())
    cands = candidate_tasks(st, only_children_of=only_children_of)
    if not cands:
        return []
    out = []
    seeded = 0
    for tid in cands:
        if seeded >= limit:
            print("[story] 播种达到上限 %d，还剩 %d 个候选未播（疑似任务图有环？）"
                  % (limit, len(cands) - seeded), flush=True)
            break
        ch = story_mod.start_story_task(st, tid, now)
        if ch:
            out.extend(ch)
            seeded += 1
            cfg = cfg_of(tid) or {}
            if cfg.get("activityOn"):
                # 活动系统未实现，无法判定 isActivityValid；按文档策略放行并留痕
                print("[story] 播种任务 %s 带 activityOn=%s（活动系统未实现，按开启处理）"
                      % (tid, cfg.get("activityOn")), flush=True)
    if seeded:
        print("[story] 播种 %d 个任务（候选 %d 个）" % (seeded, len(cands)),
              flush=True)
    return out


def _broken_pre_tasks():
    """PreTask 在 StoryTaskData 和 TaskData 里都查不到的清单（真断链）。

    ★ 只算「两张表都没有」的。指向 TaskData 的是跨表前置（PreTaskType=1），
      那是正常数据，不是缺陷 —— seed_cross_table 专门处理它。
    """
    _build_index()
    from .. import gamedata as _gd
    T = _gd.table("TaskData") or {}
    T = {int(v["uiD"]) for v in T.values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    out = []
    for t, v in (_INDEX or {}).items():
        pre = v.get("PreTask")
        if pre is None:
            continue
        try:
            pre = int(pre)
        except Exception:
            continue
        if pre not in (_INDEX or {}) and pre not in T:
            out.append((t, pre))
    return sorted(out)


def _dangling_task_data_ids():
    """被 type3/7 任务引用但 TaskData 里查不到的 id（数据缺陷，只算一次）。"""
    _build_index()
    from .. import gamedata as _gd
    T = _gd.table("TaskData") or {}
    T = {int(v["uiD"]): v for v in T.values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    out = []
    for t, v in (_INDEX or {}).items():
        if v.get("TaskType") not in (3, 7):
            continue
        for pid in (v.get("Parameter1") or []):
            try:
                pid = int(pid)
            except Exception:
                continue
            if pid not in T:
                out.append((t, pid))
    return sorted(out)


_WARNED = set()


def _warn_once(key, msg):
    if key in _WARNED:
        return
    _WARNED.add(key)
    _log.info("[story] " + msg)


def rescan(st, now=None, limit=SEED_LIMIT):
    """阶段 D：把「没人触发」的任务重新检查一遍，返回同步描述列表。

    ★ 为什么需要它（阶段 C 实测后发现）

      PostTasks 与 PreTask 字段 958/958 完全一致，所以「线性推进」覆盖了绝大多数任务。
      真正漏的只有三类：

        1. **跨表前置**（PreTaskType=1，6 条）：上游是 TaskData，
           StoryTaskData.PostTasks 不会启动它。seed_cross_table 以前只在登录时调，
          通用任务完成后不会触发 -> 这 6 条链永远不动。
        2. **链式闸门**（TaskType 6，24 条）：判定在 check_chain_tasks，
           由 events.tick 调用；tick 只在登录和副本结算时跑，
           通用任务完成后不跑。
        3. **断链**（400006 -> 1124 不存在）：扫描必须容错，不能崩。

    ★ 不是「扫描 1047 条找下一步」——线性推进走 PostTasks 就够。
      这里只补上面三类。

    幂等：重复调用不产生重复帧（candidate_tasks / check_chain_tasks 都自带判断）。
    """
    now = int(now if now is not None else __import__("time").time())
    out = []

    # 数据缺陷告警（一次性）
    for t, pre in _broken_pre_tasks():
        _warn_once("broken:%d" % t,
                   "数据缺陷：任务 %s 的 PreTask=%s 在 StoryTaskData/TaskData 里都不存在，"
                   "该任务永远不可达" % (t, pre))
    dangling = _dangling_task_data_ids()
    if dangling:
        _warn_once("dangling",
                   "数据缺陷：%d 个 type3/7 任务引用的 TaskData id 不存在，"
                   "这些任务的完成条件无法判定（例：%s）"
                   % (len(dangling), dangling[:5]))

    # ① 跨表前置
    try:
        out.extend(seed_cross_table(st, now))
    except Exception as e:
        _log.warning("[story] rescan 跨表播种失败: %s" % e)

    # ② 链式闸门（TaskType 6）
    from . import events as _events
    try:
        tick_res = _events.tick(st, now)
        hits = list(tick_res.chain_hits)
        if hits:
            from .rpc import _finish_story_tasks
            out.extend(_finish_story_tasks(st, hits, source="chain"))
    except Exception as e:
        _log.warning("[story] rescan 链式检查失败: %s" % e)

    return out


def seed_cross_table(st, now=None):
    """跨表前置的增量播种：TaskData 完成后，检查哪些剧情任务的 PreTask 指向它。

    全表只有 5 条（400101<-30 / 415601<-510052 / 416404<-510047 /
    416407<-510048 / 416410<-510049）。这 5 条链共 14 条任务，
    不补这条边就永远不动。
    """
    _build_index()
    out = []
    for tid, cfg in (_INDEX or {}).items():
        if cfg.get("PreTaskType") != 1:
            continue
        pre = cfg.get("PreTask")
        if pre is None:
            continue
        try:
            pre = int(pre)
        except Exception:
            continue
        bag = st.props.get("story_task_bag")
        if isinstance(bag, dict) and tid in bag:
            continue
        done = st.props.get("done_story_tasks")
        if isinstance(done, dict) and tid in done:
            continue
        from .. import tasks as _tasks
        if _tasks.task_state(st.props, pre) != TASK_STATE_FINISHED:
            continue
        ch = story_mod.start_story_task(st, tid, int(now or time.time()))
        if ch:
            out.extend(ch)
            print("[story] 跨表前置满足，播种任务 %s（PreTask=TaskData[%s]）"
                  % (tid, pre), flush=True)
    return out
