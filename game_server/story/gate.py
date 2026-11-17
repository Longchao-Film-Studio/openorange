#!/usr/bin/env python3
"""game_server.story.gate —— 交互前提中间件（InteractGate）。

## 这个模块凭什么存在

客户端判断「一个交互/任务能不能出现」有一套完整规则，服务端**一条都不认**。
于是服务端把任务派下去、客户端直接拒绝渲染，玩家看到的是「任务在列表里，
但走到地方什么都没有」，而且没有任何报错，两端日志都干净。

2026-11-04 的短信事故就是这样：1040「"演员"已就位」带 LevelOn=3，
新号 level=1，客户端 streetactorbase.lua:554-557 判 level >= LevelOn 不通过，
NPC 13001 根本不出现，整条主线（含 1044 那条短信）全部不可达。

## 客户端到底看什么（streetactorbase.lua:541-615，7 条 AND）

    canShowInteract = systemTaskSatisfy      -- interactData.preTaskSystem
                  and taskSatisfy            -- interactData.interactTask
                                             --   + StoryTaskData[task].LevelOn
                                             --   + getStoryTaskStartTime < 服务器时间
                  and levelSatisfy           -- interactData.interactLevel
                  and timeSatisfy            -- interactData.interactTime（客户端空实现）
                  and charSatisfy            -- interactData.interactChar
                  and preInteractSatisfy     -- interactData.preInteract
                  and activityOn             -- interactData.activityOn

全 1647 条交互里在用的前提：
    interactTask 928 / activityOn 90 / preInteract 13 / preTaskSystem 2 / interactChar 2

## 本模块做什么 / 不做什么

做：回答「服务端准备推进这个任务时，客户端会不会认」，以及不认的**原因**。
不做：修数据、发经验、改等级。它只让问题**可见**。

★ 这是判定层，不是补偿层。经验/等级缺口由各自模块按本层的输出决定，
  不在这里偷偷塞奖励 —— 否则就变成打补丁了。

## 三种策略（默认 warn）

    warn   打日志 + 落 tsv，照常派发        <- 默认，用于观测
    block  不派发，玩家停在当前任务
    force  完全不检查（兼容旧行为）

★ 默认 warn 而不是 block：block 会让玩家停在原地，而观测前并不知道
  有多少任务依赖当前行为。先量化，再决定拦不拦。

## 不变量（tests/test_story_gate.py 逐条可红）

    INV-GATE-1  判定只读，绝不修改 props
    INV-GATE-2  前提字段来自客户端数据，不在代码里手抄
    INV-GATE-3  why_not 必须给出可读原因，不能只返回 bool
    INV-GATE-4  warn 模式下 can_advance 的结果不影响派发
"""
import os
import time
from typing import Any

from shared.log_tee import get_logger

from .. import gamedata

_log = get_logger(__name__)

# ---------------------------------------------------------------- 策略

MODE_WARN = "warn"
MODE_BLOCK = "block"
MODE_FORCE = "force"


def _mode() -> str:
    """当前策略。环境变量 OO_STORY_GATE 覆盖，默认 warn。"""
    v = (os.environ.get("OO_STORY_GATE") or MODE_WARN).strip().lower()
    return v if v in (MODE_WARN, MODE_BLOCK, MODE_FORCE) else MODE_WARN


# ---------------------------------------------------------------- 数据读取

def _interact_table() -> dict:
    """StreetActorInteractData，展平成 {interactId: 条目}。

    BDD 里的形状是 {actorGroupId: {"normal"/"extra": {interactId: {...}}}}，
    两个分支都收 —— 客户端 getTriggerEvent 优先用 extra，两者都得认识。
    """
    raw = gamedata.table("StreetActorInteractData") or {}
    out: dict[int, dict] = {}
    for group in raw.values():
        if not isinstance(group, dict):
            continue
        for branch in group.values():
            if not isinstance(branch, dict):
                continue
            for iid, entry in branch.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    out[int(entry.get("interactId") or iid)] = entry
                except (TypeError, ValueError):
                    continue
    return out


def interact_of_task(task_id: int) -> list[dict]:
    """这个任务对应的交互条目（TaskType==2 时 Parameter1 就是 interactId 列表）。"""
    cfg = gamedata.story_task(int(task_id)) or {}
    if int(cfg.get("TaskType") or -1) != 2:
        return []
    table = _interact_table()
    out = []
    for iid in cfg.get("Parameter1") or []:
        try:
            entry = table.get(int(iid))
        except (TypeError, ValueError):
            continue
        if entry:
            out.append(entry)
    return out


# ---------------------------------------------------------------- 判定

def _story_state(props: dict, task_id: int) -> int:
    """任务状态，语义对齐客户端 getTaskStateByTaskID（不在包里且未归档 = -1）。"""
    bag = props.get("story_task_bag")
    if isinstance(bag, dict):
        item = bag.get(int(task_id))
        if isinstance(item, dict):
            try:
                return int(item.get("state") or 0)
            except (TypeError, ValueError):
                return 0
    done = props.get("done_story_tasks")
    if isinstance(done, dict) and int(task_id) in done:
        return 6
    return -1


def _start_time(props: dict, task_id: int) -> int:
    bag = props.get("story_task_bag")
    if not isinstance(bag, dict):
        return 0
    item = bag.get(int(task_id))
    if not isinstance(item, dict):
        return 0
    try:
        return int(item.get("start_time") or 0)
    except (TypeError, ValueError):
        return 0


def _check_interact_task(props: dict, entry: dict) -> list[str]:
    """interactTask + LevelOn + start_time —— 客户端 taskSatisfy 那一段。"""
    need = entry.get("interactTask")
    if not isinstance(need, dict):
        return []
    cur_time = int(time.time())
    details: list[str] = []
    for raw_id, want in need.items():
        if not str(raw_id).isdigit():
            continue
        task_id = int(raw_id)
        try:
            want_state = int(want)
        except (TypeError, ValueError):
            continue
        have = _story_state(props, task_id)
        if have != want_state:
            details.append(f"任务{task_id} 需要 state={want_state}，当前 {have}")
            continue
        if want_state == 3:
            started = _start_time(props, task_id)
            # 客户端是**严格大于**（streetactorbase.lua:202）：curTime > start_time。
            # state_machine.make_story_task_item 已经把 start_time 置为 now-1，
            # 所以正常路径不会命中这里；命中说明数据是旧存档留下的。
            if started and started >= cur_time:
                details.append(
                    f"任务{task_id} start_time({started}) 不早于当前({cur_time})")
                continue
            cfg = gamedata.story_task(task_id) or {}
            level_on = cfg.get("LevelOn")
            if level_on:
                level = int(props.get("level") or 1)
                if level < int(level_on):
                    details.append(
                        f"任务{task_id} 要求 level >= {level_on}，当前 {level}")
                    continue
        return []  # 有一个条件满足，客户端就认
    return details


def _check_level(props: dict, entry: dict) -> list[str]:
    need = entry.get("interactLevel")
    if not need:
        return []
    level = int(props.get("level") or 1)
    try:
        need = int(need)
    except (TypeError, ValueError):
        return []
    if level < need:
        return [f"interactLevel 要求 {need}，当前 {level}"]
    return []


def _check_char(props: dict, entry: dict) -> list[str]:  # noqa: ARG001
    need = entry.get("interactChar")
    if not need:
        return []
    cur = props.get("character_choosen_char")
    if str(cur) != str(need):
        return [f"interactChar 要求角色 {need}，当前 {cur}"]
    return []


def _check_pre_interact(props: dict, entry: dict) -> list[str]:
    """preInteract：必须先做过某个交互。服务端记在 street_info.inter_actor_info。"""
    need = entry.get("preInteract")
    if not need:
        return []
    done_ids: set[int] = set()
    si = props.get("street_info")
    if isinstance(si, dict):
        for street in si.values():
            if not isinstance(street, dict):
                continue
            ia = street.get("inter_actor_info")
            if not isinstance(ia, dict):
                continue
            for v in ia.values():
                try:
                    done_ids.add(int(v))
                except (TypeError, ValueError):
                    continue
    try:
        need_id = int(need)
    except (TypeError, ValueError):
        return []
    if need_id not in done_ids:
        return [f"preInteract 要求先完成交互 {need_id}"]
    return []


def _check_activity(props: dict, entry: dict) -> list[str]:  # noqa: ARG001
    """activityOn：活动开启。服务端活动系统尚未实现 -> 一律视为不满足。

    ★ 刻意返回「未实现」而不是「满足」：返回满足等于假装功能存在，
      会让带 activityOn 的 90 条交互看起来正常，实际客户端一样不显示。
    """
    need = entry.get("activityOn")
    if not need:
        return []
    return ["activityOn 要求活动开启（服务端活动系统未实现）"]


def _check_system_task(props: dict, entry: dict) -> list[str]:  # noqa: ARG001
    need = entry.get("preTaskSystem")
    if not need:
        return []
    return ["preTaskSystem 需要系统任务状态（服务端未映射）"]


# ---------------------------------------------------------------- 任务级门槛
#
# 客户端 avatarstorytaskcomp.lua 有两条**任务级**判定（不是交互级），
# 命中就整条任务不显示 / 撤红点：
#
#   :779-791  checkTaskActivityOn    task.activityOn and not isActivityValid(..) -> false
#   :793-806  checkTaskLevelSatisfy  task.LevelOn and curLevel < task.LevelOn   -> false
#
# ★ LevelLimit 不在其中（那是别的系统用的字段），MissionOn 只影响红点显隐，
#   都**不**参与「任务能不能出现」的判定 —— 别把数据表里所有看起来像门槛的
#   字段都当成一堵墙，只认客户端真的读的那两个。


def _check_task_level(props: dict, task_id: int) -> list[str]:
    """checkTaskLevelSatisfy：StoryTaskData[task].LevelOn vs 玩家等级。"""
    cfg = gamedata.story_task(int(task_id)) or {}
    level_on = cfg.get("LevelOn")
    if not level_on:
        return []
    level = int(props.get("level") or 1)
    try:
        need = int(level_on)
    except (TypeError, ValueError):
        return []
    if level < need:
        return [f"任务{task_id} 要求 level >= {need}，当前 {level}"]
    return []


def _check_task_activity(props: dict, task_id: int) -> list[str]:  # noqa: ARG001
    """checkTaskActivityOn：任务级 activityOn。服务端活动系统未实现。"""
    cfg = gamedata.story_task(int(task_id)) or {}
    if not cfg.get("activityOn"):
        return []
    return [f"任务{task_id} 依赖活动开启（服务端活动系统未实现）"]


_CHECKS = (
    _check_interact_task,
    _check_level,
    _check_char,
    _check_pre_interact,
    _check_activity,
    _check_system_task,
)


def why_not(props: dict, task_id: int) -> list[str]:
    """客户端会不会拒绝这个任务。返回不满足的原因（空 = 会显示）。

    两层判定，缺一不可：
      1. 任务级（avatarstorytaskcomp.lua:779 / :793）—— LevelOn / activityOn
         命中就整条任务不出现，跟交互点长什么样无关。
      2. 交互级（streetactorbase.lua:541-615）—— 7 条前提。

    ★ 以前只做第 2 层，于是 1040 这种「任务级门槛」根本查不出来 ——
      它的交互条目本身是干净的，脏的在外层。

    INV-GATE-1：只读 props，不做任何写入。
    INV-GATE-3：返回可读字符串，不是 bool。
    """
    tid = int(task_id)
    task_reasons = _check_task_level(props, tid) + _check_task_activity(props, tid)
    entries = interact_of_task(tid)
    if not entries:
        # 非交互型任务：任务级门槛仍然是硬门槛
        return task_reasons
    if task_reasons:
        return task_reasons
    reasons: list[str] = []
    for entry in entries:
        local: list[str] = []
        for fn in _CHECKS:
            local.extend(fn(props, entry))
        if not local:
            return []  # 只要有一个交互点通，客户端就能推进
        reasons.extend(local)
    return reasons


def can_advance(props: dict, task_id: int) -> bool:
    """服务端准备把 task_id 推进到 Doing 时，客户端会不会认。"""
    return not why_not(props, task_id)


def note_advance(st: Any, task_id: int, *, source: str = "") -> bool:  # noqa: ANN401
    """任务派发前的检查点（唯一调用入口）。返回是否放行。

    策略见 _mode()。默认 warn：只记录，不改变行为。
    """
    mode = _mode()
    if mode == MODE_FORCE:
        return True
    reasons = why_not(st.props, int(task_id))
    if not reasons:
        return True
    cfg = gamedata.story_task(int(task_id)) or {}
    name = cfg.get("TaskName") or ""
    _log.warning("[gate] 任务 %s「%s」客户端会拒绝（%s）：%s",
                 task_id, name, source or "?", "; ".join(reasons))
    _record_tsv(task_id, name, reasons, source)
    return mode != MODE_BLOCK


def _tsv_path() -> str:
    from .. import config
    return os.path.join(getattr(config, "LOG_DIR", "var/logs"), "story_gate.tsv")


_SEEN: set[int] = set()


def _record_tsv(task_id: int, name: str, reasons: list[str], source: str) -> None:
    """落盘（进程内按 task 去重，避免刷屏；日志里仍是每次一条）。"""
    if task_id in _SEEN:
        return
    _SEEN.add(task_id)
    try:
        path = _tsv_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write("ts\ttask_id\tname\tsource\treasons\n")
            f.write("{}\t{}\t{}\t{}\t{}\n".format(
                int(time.time()), task_id, name, source, " | ".join(reasons)))
    except Exception as e:  # noqa: BLE001 —— 落盘失败绝不能影响任务推进
        _log.warning("[gate] 落盘失败: %s", e)


# ---------------------------------------------------------------- 审计

def audit_all(props: dict, limit: int = 0) -> list[dict]:
    """扫全表：当前玩家状态下，哪些进行中的任务客户端会拒绝。

    ★ 量化工具，不是修复工具 —— 先看清有多少条会卡，再决定怎么补。
    """
    bag = props.get("story_task_bag")
    if not isinstance(bag, dict):
        return []
    rows: list[dict] = []
    for key, item in bag.items():
        if not isinstance(key, int) or not isinstance(item, dict):
            continue
        if int(item.get("state") or 0) != 3:
            continue
        reasons = why_not(props, key)
        if not reasons:
            continue
        cfg = gamedata.story_task(key) or {}
        rows.append({"task_id": key, "name": cfg.get("TaskName"),
                     "type": cfg.get("TaskType"), "reasons": reasons})
        if limit and len(rows) >= limit:
            break
    return rows
