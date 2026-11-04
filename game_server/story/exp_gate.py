#!/usr/bin/env python3
"""game_server.story.exp_gate —— 主线等级门槛的经验补齐（架构级规则）。

## 这个模块凭什么存在

官方设计里**主线剧情任务一分经验都不发**。全表 184 条发道具 2「执业资历」的任务是：
    头衔挑战 TaskType=10  175 条
    头衔日常 TaskType=9     8 条
    新手引导 TaskType=1     1 条（5000004「完成1次2v2」）
主线 StoryTaskData 一条都没有。

但主线里有 **12 条任务带 LevelOn**（3 / 7 / 9 / 10 / 12 / 15 / 17 / 18 / 19 / 22 / 32 / 32）。
打完主线要 32 级，而玩家在新号（level=1, exp=0）状态下**永远升不上去** ——
客户端 streetactorbase.lua:554-557 / avatarstorytaskcomp.lua:793-806 判定等级不足，
任务不显示、交互点不出现，主线和它下面挂的所有内容（含短信）全部不可达。

## 规则（不是补丁）

    「任务 A 完成后要接的任务 B 带 LevelOn = L，
      而玩家当前等级 < L 时，补发经验让玩家升到 L。」

这条规则对**任何**带 LevelOn 的任务生效，不认识 1040、也不认识任何一个具体 id。
将来策划在任意位置加一条 LevelOn 门槛，本模块自动覆盖。

## 边界（刻意不做的事）

    · 不直接改 level —— 只发道具 2，走既有链路
      bag.grants -> effects.apply_auto_use -> props["exp"] += N -> level.upgrade()
      等级只能由 level.py 一个入口改（INV-LV-1）
    · 不做「自动升级」—— 客户端 directortitlev3panel.lua:265-270 在
      经验够了时会自己调 Cmd_UpgradeLevel_CS，服务端 level.upgrade 接得住
    · 不补已完成的存档 —— 只对「即将派发」的任务生效
    · 活动门槛（activityOn）不在这里补 —— 那需要真做活动系统，不是发经验能解决的

## 开关

    OO_STORY_EXP_GATE=0  关掉（排查用）
    默认开启

## 不变量（tests/test_story_exp_gate.py 逐条可红）

    INV-EXP-1  只发道具 2，绝不直接写 level / exp
    INV-EXP-2  等级已够时一封信都不发（幂等且不浪费）
    INV-EXP-3  补发的量正好够升到 LevelOn，不多发
    INV-EXP-4  任何异常都不阻断任务推进
"""
import os
from typing import Any, Protocol

from shared.log_tee import get_logger

from .. import gamedata, level
from ..property.bag import grants
from ..property.tx import Tx

_log = get_logger(__name__)


class StateLike(Protocol):
    """player_state.PlayerState 的最小接口（与 level.StateLike 同款）。

    ★ 为什么不直接 import PlayerState：领域层不该反向依赖存档实现。
    ★ 为什么不用 Any：ANN401 禁止动态类型，而且 Protocol 能把
      "我只需要 props 和 save" 这件事表达清楚。
    """

    props: dict[str, Any]

    def set(self, name: str, value: Any) -> Any: ...  # noqa: ANN401

    def next_property_id(self) -> int: ...

    def save(self) -> object: ...

#: 经验道具 id（ItemData[2]「执业资历」，useType=avatarAttr autoUse）
EXP_ITEM_ID = 2


def _enabled() -> bool:
    return (os.environ.get("OO_STORY_EXP_GATE") or "1").strip() != "0"


def exp_to_reach(cur_level: int, target_level: int, cur_exp: int = 0) -> int:
    """从 cur_level 升到 target_level 还需要多少经验。

    ★ 必须扣掉玩家**已经攒着的 exp**（cur_exp）—— 这是「重复调用会无限发经验」
      这个 bug 的唯一堵法：
      纯按等级差额算的话，玩家 exp=500、level=1 时算出「还差 500」，
      再发 500，exp 变 1000 —— 但玩家其实只要升一次级就够了。
      客户端在 directortitlev3panel.lua:265-270 看到 exp >= expNeed 会自己调
      Cmd_UpgradeLevel_CS，服务端 level.upgrade 把 exp 扣掉；
      在那之前重复调用，纯等级算法就会一直发。

    算法：从最低的「exp 已经够升」的等级开始往上推。
    """
    cur_level = max(1, int(cur_level))
    target_level = max(1, int(target_level))
    if target_level <= cur_level:
        return 0
    # 先把 cur_exp 转换成「已经能升到几级」—— 玩家攒着没升的经验也算数
    lv = cur_level
    pool = max(0, int(cur_exp))
    while lv < target_level:
        need = level.exp_need(lv)
        if need is None:
            break
        need = int(need)
        if pool >= need:
            pool -= need
            lv += 1
            continue
        return need - pool + exp_to_reach(lv + 1, target_level)
    return 0


def ensure_for_task(st: StateLike, task_id: int, *, source: str = "") -> list[bytes]:
    """检查 task_id 的 LevelOn，等级不够就补经验。返回同步帧。

    ★ 唯一调用点：任务即将被推进到 Doing 之前（story/rpc.py 的 PostTasks 处）。
    ★ 任何失败都不允许阻断任务推进（INV-EXP-4）。
    """
    if not _enabled():
        return []
    try:
        return _ensure_inner(st, int(task_id), source=source)
    except Exception as e:  # noqa: BLE001 —— 补经验失败绝不能卡住剧情
        _log.warning("[exp_gate] 任务 %s 经验补齐失败（不影响推进）: %s", task_id, e)
        return []


def _ensure_inner(st: StateLike, task_id: int, *, source: str) -> list[bytes]:
    cfg = gamedata.story_task(task_id) or {}
    level_on = cfg.get("LevelOn")
    if not level_on:
        return []
    try:
        need_level = int(level_on)
    except (TypeError, ValueError):
        return []

    cur_level = int(st.props.get("level") or 1)
    if cur_level >= need_level:
        return []  # INV-EXP-2

    cur_exp = int(st.props.get("exp") or 0)
    gap = exp_to_reach(cur_level, need_level, cur_exp)
    frames: list[bytes] = []
    if gap > 0:
        tx = Tx(st.props)
        out = grants.grant(st.props, EXP_ITEM_ID, gap, tx=tx, source=f"gate:{source}")
        if not out.ok:
            _log.warning("[exp_gate] 任务 %s 补发经验失败: %s", task_id, out.error)
            return []
        frames.extend(tx.frames())
        _log.info("[exp_gate] 任务 %s 需要 Lv%s（当前 Lv%s），补发 %s 经验（%s）",
                  task_id, need_level, cur_level, gap, source or "?")

    # ---------------------------------------------------------------
    # ★ 光发经验不够：经验只是攒在 exp 里，等级还是原来那个。
    #   客户端 directortitlev3panel.lua:265-270 只在**打开导演面板**时
    #   才自动调 Cmd_UpgradeLevel_CS —— 玩家不去点面板就永远卡在低等级。
    #   所以这里必须把经验真正换成等级。
    #
    #   ★ 但不许直接写 props["level"]（INV-LV-1）：等级只能由
    #     level.upgrade() 一个入口改，它负责扣 exp、累加 all_exp、
    #     重算体力上限、发 AvatarAttr 事件。
    #     这里只是「替玩家点一下升级按钮」，规则完全复用。
    # ---------------------------------------------------------------
    ups = 0
    while int(st.props.get("level") or 1) < need_level:
        res = level.upgrade(st)
        if not res.ok:
            _log.warning("[exp_gate] 任务 %s 升级停在 Lv%s: %s",
                         task_id, st.props.get("level"), res.reason)
            break
        frames.extend(res.frames or [])
        ups += 1
    if ups:
        _log.info("[exp_gate] 任务 %s 替玩家升了 %s 级 -> Lv%s",
                  task_id, ups, st.props.get("level"))
    return frames


def scan_upcoming_level_gates(task_id: int) -> list[dict]:
    """这个任务的 PostTasks 里，哪些带 LevelOn。用于审计/日志。"""
    cfg = gamedata.story_task(int(task_id)) or {}
    out = []
    for nxt in cfg.get("PostTasks") or []:
        try:
            nid = int(nxt)
        except (TypeError, ValueError):
            continue
        ncfg = gamedata.story_task(nid) or {}
        if ncfg.get("LevelOn"):
            out.append({"task_id": nid, "level_on": int(ncfg["LevelOn"]),
                        "name": ncfg.get("TaskName")})
    return out


def audit_mainline() -> list[dict]:
    """列出全表所有带 LevelOn 的剧情任务及其累计经验需求（运维/文档用）。"""
    rows: list[dict] = []
    seen: set[int] = set()
    # 只扫实际存在的 id：StoryTaskData 的键就是 taskId
    table = gamedata.table("StoryTaskData") or {}
    for key, row in table.items():
        if not isinstance(row, dict):
            continue
        level_on = row.get("LevelOn")
        if not level_on:
            continue
        try:
            tid = int(row.get("TaskID") or key)
            lv = int(level_on)
        except (TypeError, ValueError):
            continue
        if tid in seen:
            continue
        seen.add(tid)
        rows.append({
            "task_id": tid,
            "name": row.get("TaskName"),
            "level_on": lv,
            "cum_exp": exp_to_reach(1, lv),
        })
    rows.sort(key=lambda r: r["level_on"])
    return rows
