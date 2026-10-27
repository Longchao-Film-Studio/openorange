#!/usr/bin/env python3
"""game_server.property.level —— 玩家等级的三条 RPC（H3）。

## 客户端契约（三条都只看 ret.status）

`entities/avatarcomponent/avatarlevelcomp.lua`:

    :47-49  Cmd_UpgradeLevel_CS(cb)           serverRpc("Cmd_UpgradeLevel_CS", {})
    :51-55  Cmd_UpgradeLevel_SC(cb, ret)      if ret.status and cb then cb() end
    :57-59  Cmd_FetchLevelupAward_CS(level)   serverRpc("Cmd_FetchLevelupAward_CS", {level})
    :71-79  Cmd_FetchLevelupAward_SC          if not ret.status then return end；
                                              RDC:SetRed(PlayerLevel, hasAvailableLevelupAward())
    :61-63  Cmd_FetchTitelVitAward_CS(cb)     serverRpc("Cmd_FetchTitelVitAward_CS", {})
    :65-69  Cmd_FetchTitelVitAward_SC         if ret.status and cb then cb() end

**客户端不做任何校验** —— 能不能升、扣多少经验、奖励领过没有，全由服务端判。
所以这里的每一处 `status=False` 都是「服务端拒绝」，必须给得出理由（写日志）。

## 为什么升级要发事件 + 重算解锁

升级会让两样东西失效：
  1. `AvatarAttr{level}` 事件 —— 剧情 7006/7007 靠它推进（`ev_avatar_attr` 不按 level 去重）；
  2. `AllSystemCatalogueData` 里 `unlockType=3` 的 21 条系统入口（`level >= unlockParam`）。

两件事都由本模块在**同一个应答**里做完，客户端不需要重登：
`recalc_system_unlocks` 是解锁的唯一写入口，它返回的帧直接拼进 syncs。

## 为什么三条 RPC 都走 respond_after_sync

帧顺序不变量（docs/规范-帧顺序约定.md）：客户端收到应答帧立刻执行回调，
回调里读的属性必须已经同步过。`Cmd_UpgradeLevel_SC` 的回调会开升级面板并读
`GlobalData.Avatar.level`，所以 level/exp 的同步帧**必须**排在应答之前。
"""
from shared.log_tee import get_logger

from .. import gamedata, level
from ..rpc_common import decode_params, respond_after_sync, state
from ..story import events as _events
from ..story.tasks import drain_event_task_sync

_log = get_logger(__name__)

#: `ret.status=False` 的统一形状（客户端只读 status）
RET_FAIL = {"status": False}


def _int_param(vals, idx, default=None):
    if not (isinstance(vals, list) and len(vals) > idx):
        return default
    try:
        return int(vals[idx])
    except (TypeError, ValueError):
        return default


def _upgrade_level(rid, _params, uid=None):
    """Cmd_UpgradeLevel_CS {} —— 升一级。

    校验顺序（任一失败都只回 status=False，**0 个同步帧**）：
      1. 满级（LevelExpData 里没有 level+1）
      2. 经验不足（防伪造；客户端面板会自动触发，但服务端是裁判）

    成功时帧顺序：exp/level/all_exp 同步 -> 系统解锁同步 -> 应答。
    """
    st = state(uid)
    res = level.upgrade(st)

    if not res.ok:
        _log.info("[level] 升级被拒：%s（level=%s exp=%s）",
                  res.reason, res.old_level, res.old_exp)
        st.save()
        return respond_after_sync(rid, [], ret=dict(RET_FAIL))

    syncs = list(res.frames)

    # 等级变化 -> AvatarAttr 事件（剧情 7006/7007 等；刻意不去重，见 ev_avatar_attr 注释）
    try:
        ev = _events.apply_event(
            st, _events.ev_avatar_attr("level", res.new_level, source="rpc:Cmd_UpgradeLevel_CS"))
        syncs.extend(drain_event_task_sync(st, ev))
    except Exception as e:                       # noqa: BLE001
        _log.warning("[level] 升级后发 AvatarAttr 事件失败：%s", e)

    # ★ unlockType=3 的 21 条系统入口在这里**不需要**单独再算一次：
    #   上面的 apply_event 内部第 5 步已经调 recalc_system_unlocks 并把帧
    #   放进 EventResult.frames，drain_event_task_sync 会把它们吐出来。
    #   本文件早期版本额外调了一次 —— 破坏性验证时发现删掉它测试仍全绿，
    #   说明它是纯冗余的第二写入口（违反 R8）。已删。
    #
    #   实测证据（2026-10-28）：把 apply_event 那一行删掉 -> 解锁断言转红；
    #   把下面这段历史冗余删掉 -> 全绿。所以真正的写入口只有 apply_event 一条。

    st.save()
    _log.info("[level] 升级成功 %s -> %s，帧 %s 条", res.old_level, res.new_level, len(syncs))
    return respond_after_sync(rid, syncs)


def _fetch_levelup_award(rid, params, uid=None):
    """Cmd_FetchLevelupAward_CS {level} —— 领某一级的升级奖励（幂等）。

    客户端传的是**它想领的等级**（playerphasepanel.lua:358 的 `self.curSelectedLevel`）。
    拒绝条件：
      - 参数不是整数 / 缺参
      - level < 1 或 level > 当前等级（不能预支）
      - 该级 LevelExpData.awardItem 为空（Lv1 没有奖励）
    已领过（levelup_awards[level] == 1）**回成功但不发道具**（客户端会重复点）。
    """
    vals = decode_params(params)
    lv = _int_param(vals, 0)
    st = state(uid)
    if lv is None:
        _log.info("[level] 领奖被拒：参数非法 %r", vals)
        return respond_after_sync(rid, [], ret=dict(RET_FAIL))

    res = level.grant_level_award(st, lv)
    if not res.ok:
        _log.info("[level] 领 %s 级奖励被拒：%s", lv, res.reason)
        st.save()
        return respond_after_sync(rid, [], ret=dict(RET_FAIL))

    st.save()
    if res.already:
        return respond_after_sync(rid, [])
    _log.info("[level] 领 %s 级奖励成功：%s", lv, res.granted)
    return respond_after_sync(rid, list(res.frames))


def _fetch_titel_vit_award(rid, _params, uid=None):
    """Cmd_FetchTitelVitAward_CS {} —— 领「消耗体力」循环任务奖励。

    ★ 本阶段只做**安全失败**（方案 §七 B1）。

    依据 `ui/module/directortitle/directortitlehelper.lua:289-323` 的 `fakeTask`：

        消耗 CommonConstData.TitleLoopTaskCost = 120 点体力 -> 可领 1 次
        奖励 = TitleLoopTaskReward = [2, 120]（120 点执业经验）
        每日上限 TitleLoopTaskDailyLimit = 6（38 级以上才限，见 TitleLoopTaskLimitUnlockLevel）

    服务端**从来没有扣过体力**（`consume_vit` 全仓零写入点），
    所以 `fakeTask` 永远 state=Doing，这个 RPC 正常玩不会触发。

    两种选择：
      a) 回 status=true 但不发奖 -> 客户端 cb 执行、UI 显示领到了，实际没有（**假成功**，雷1 教训）；
      b) 回 status=false -> 客户端 cb 不执行，什么都不显示。

    选 (b)：宁可功能不可用，不可假成功。等体力消耗系统上线后在这里补成功分支。
    """
    st = state(uid)
    cost = _int(gamedata.const("TitleLoopTaskCost", 120), 120)
    have = _int(st.props.get("consume_vit"), 0)
    _log.info("[level] 体力循环领奖被拒（B1 未实现）：consume_vit=%s < %s", have, cost)
    return respond_after_sync(rid, [], ret=dict(RET_FAIL))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "RET_FAIL",
    "_fetch_levelup_award",
    "_fetch_titel_vit_award",
    "_upgrade_level",
]
