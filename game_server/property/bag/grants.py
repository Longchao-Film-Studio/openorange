"""道具发放（**唯一入口**）。

任何会往 item_bag 写东西的路径都必须走这里：
    剧情/宝箱/副本结算/GM（现在走 story.add_item）
    道具效果里的 item / randomItem / selectItem
    邮件附件（雷1 上线后）
    管理接口 /player/grant

## 职责

    · 堆叠规则：notStack 不叠加；同 itemId 且未满 maxNum 才合并
    · 上限：超过 maxNum 拒绝并返回 Bag_MaxNumExceed（邮件未实现期间的决策，
      雷1 上线后改成转邮件，调用点不变）
    · 钥匙：keys.alloc()（永不回收）
    · 结构：factory.new_instance(props, "ItemInfo")，ext 容器自带 __id__
    · 过期：ItemData.validTime（小时）-> expireTime
    · autoUse：获得即使用（递归深度上限，防环）

## 为什么返回 outcome 而不是帧

帧的形态由调用方决定（有的走 propertySync，有的走 entity response），
这里只负责「改 props + 登记 tx ops」，帧由 tx.frames() 统一生成。
"""
from dataclasses import dataclass, field
from time import time
from typing import Any, cast

from shared.log_tee import get_logger

from ... import gamedata
from .. import factory
from ..tx import Tx
from . import effects, keys, report
from .errors import BAG_INVALID_ITEM_ID, BAG_INVALID_NUM, BagError

_log = get_logger(__name__)

#: autoUse 递归上限（防「A 用出 A」这类配置错误把服务器转死）
MAX_AUTO_USE_DEPTH = 8


@dataclass
class GrantOutcome:
    """一次发放的结果。ok=False 时 error 必有值。"""

    ok: bool
    item_id: int = 0
    count: int = 0
    #: 受影响的格子键（新建或叠加）
    grids: list[str] = field(default_factory=list)
    #: 其中新建的个数
    new_grids: int = 0
    #: 新建的键（同步时用 OP_ADD；叠加的用 OP_CHANGE）
    new_grid_keys: list[str] = field(default_factory=list)
    #: 实际入包数量（autoUse 的应用掉的不算入包）
    stored: int = 0
    #: autoUse 应用掉的数量
    auto_used: int = 0
    error: BagError | None = None


def item_bag(props: dict[str, Any]) -> dict[str, Any]:
    """取 item_bag 容器；不存在视为结构损坏（初始快照一定有）。"""
    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        raise BagError(BAG_INVALID_ITEM_ID, "item_bag 不存在（初始快照缺该属性）")
    return bag


def _limit(cfg: dict[str, Any]) -> int | None:
    """单格上限；**None = 不限**。

    ★ 配表里 2,728 条有 648 条没配 maxNum —— 不能当成 1，否则发 500 个会被拆成
      500 个格子（客户端格子有限，同步帧爆炸）。"没配"的语义是"没有上限"，
      与老实现（无条件叠加）一致。
    """
    try:
        n = int(cfg.get("maxNum") or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else None


def _is_stackable(cfg: dict[str, Any]) -> bool:
    return not bool(cfg.get("notStack"))


def _expire_time(cfg: dict[str, Any], now: int) -> float:
    """按 validTime（小时）算过期时间；没配则永久（0）。"""
    try:
        hours = float(cfg.get("validTime") or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours <= 0:
        return 0.0
    return float(now + int(hours * 3600))


def _new_grid(props: dict[str, Any], item_id: int, count: int, cfg: dict[str, Any]) -> dict[str, Any]:
    """按客户端 ItemInfo 声明建一个干净格子（ext 容器自带 __id__）。"""
    grid = cast("dict[str, Any]", factory.new_instance(props, "ItemInfo"))
    key = keys.alloc(props)
    grid["id"] = key
    grid["itemId"] = int(item_id)
    grid["num"] = int(count)
    grid["getTime"] = float(time())
    grid["expireTime"] = _expire_time(cfg, int(time()))
    return grid


def _find_stack(props: dict[str, Any], item_id: int, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """找一个可叠加的已有格子（同 itemId、可叠、未满）。"""
    if not _is_stackable(cfg):
        return None
    limit = _limit(cfg)
    bag = item_bag(props)
    for grid in bag.values():
        if not isinstance(grid, dict):
            continue
        if int(grid.get("itemId") or 0) != int(item_id):
            continue
        num = int(grid.get("num") or 0)
        if limit is None or num < limit:
            return grid
    return None


def grant(
    props: dict[str, Any],
    item_id: int,
    count: int,
    *,
    tx: Tx,
    source: str = "",
    depth: int = 0,
) -> GrantOutcome:
    """发放 item_id x count。

    tx 必传：同一批发放要合并成一次同步（剧情一晚发 5 件东西不该推 5 次全量）。
    """
    item_id = int(item_id)
    try:
        count = int(count)
    except (TypeError, ValueError):
        return GrantOutcome(False, item_id, 0, error=BagError(BAG_INVALID_NUM, "数量非法"))
    if count <= 0:
        return GrantOutcome(False, item_id, 0, error=BagError(BAG_INVALID_NUM, "数量必须为正"))

    cfg = gamedata.item(item_id)
    if not isinstance(cfg, dict) or not cfg:
        # 配表里引用了非道具 id（例如宝箱奖励里的装备 id 4205 在 EquipmentData）
        # —— 背包造不出来，落盘等对应系统上线，绝不静默丢。
        report.note("item_id", item_id, "不在 ItemData（可能是装备/时装等非道具实例）")
        return GrantOutcome(False, item_id, count,
                            error=BagError(BAG_INVALID_ITEM_ID, "ItemData 里没有这个道具"))

    # ---- autoUse：获得即使用（不建格子）----
    if cfg.get("autoUse") and depth < MAX_AUTO_USE_DEPTH:
        use_type = gamedata.item_use_type(item_id)
        if effects.supports(use_type):
            applied = effects.apply_auto_use(props, item_id, count, tx=tx, source=source, depth=depth)
            if applied is not None:
                return GrantOutcome(True, item_id, count, auto_used=count)
    elif cfg.get("autoUse"):
        _log.warning("[bag] 道具 %s 是 autoUse 但嵌套超过 %s 层，按普通入库处理",
                     item_id, MAX_AUTO_USE_DEPTH)

    limit = _limit(cfg)
    remaining = count
    grids: list[str] = []
    new_grid_keys: list[str] = []
    new_grids = 0
    stored = 0

    # ---- 先叠加 ----
    stack = _find_stack(props, item_id, cfg)
    if stack is not None:
        key = str(stack.get("id") or "")
        room = remaining if limit is None else limit - int(stack.get("num") or 0)
        put = min(room, remaining)
        if put > 0:
            stack["num"] = int(stack.get("num") or 0) + put
            # ★ 整格 set（不是子字段）：客户端 _changeSubProperty 按「name=格子键，
            #   value=整格」匹配 __id__ 后原地 rochange；写子字段会变成
            #   propertyid=格子__id__ / name="num"，客户端找不到对应容器。
            tx.set(("item_bag", key), stack)
            remaining -= put
            stored += put
            if key:
                grids.append(key)

    # ---- 再开新格 ----
    while remaining > 0:
        put = remaining if limit is None else min(limit, remaining)
        grid = _new_grid(props, item_id, put, cfg)
        key = str(grid["id"])
        tx.set(("item_bag", key), grid)
        remaining -= put
        stored += put
        new_grids += 1
        grids.append(key)
        new_grid_keys.append(key)
        if new_grids > 9999:                       # 防御：配置 maxNum=0 之类的病态数据
            _log.error("[bag] 发放 %s 时开格数异常，中止", item_id)
            break

    _log.info("[bag] 发放 %s x%s（%s）-> 新格 %s 个，入包 %s，autoUse %s",
              item_id, count, source or "-", new_grids, stored, count - stored)
    return GrantOutcome(True, item_id, count, grids=grids,
                        new_grids=new_grids, new_grid_keys=new_grid_keys,
                        stored=stored, auto_used=count - stored)


def grant_many(
    props: dict[str, Any],
    items: dict[int, int],
    *,
    source: str = "",
) -> tuple[Tx, list[GrantOutcome]]:
    """批量发放（宝箱/结算/管理接口的典型形态）：同一个 Tx，一次同步。

    返回 (tx, outcomes)；调用方用 sync_frames() 生成帧，
    并在同一个 tx 上追加其它变更（例如剧情事件帧）。
    """
    tx = Tx(props)
    outcomes: list[GrantOutcome] = []
    for iid, cnt in items.items():
        outcomes.append(grant(props, iid, cnt, tx=tx, source=source))
    return tx, outcomes


def sync_frames(props: dict[str, Any], outcomes: list[GrantOutcome]) -> list[bytes]:
    """把发放结果转成 item_bag 属性同步帧（OP_ADD 新格 / OP_CHANGE 叠加）。

    ★ 收敛点：以前剧情发奖/宝箱/结算/管理接口各自拼这段（4 份重复代码），
      而且都要自己从 props 里摸 bag.__id__。
    """
    from ... import ms as msmod
    from ...rpc_common import SYNC_SELF, sync_frame

    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        return []
    pid = bag.get("__id__") or 0
    frames: list[bytes] = []
    for oc in outcomes:
        if not oc.ok:
            continue
        for key in oc.grids:
            grid = bag.get(key)
            if not isinstance(grid, dict):
                continue
            op = msmod.OP_ADD if key in oc.new_grid_keys else msmod.OP_CHANGE
            frames.append(sync_frame(SYNC_SELF, pid, op, str(key), grid))
    return frames



def grant_frames(props: dict[str, Any], item_id: int, count: int, source: str = "") -> tuple[list[bytes], GrantOutcome]:
    """便利包装：建 Tx、发放、返回帧（单次发放场景用）。"""
    tx = Tx(props)
    outcome = grant(props, item_id, count, tx=tx, source=source)
    return tx.frames(), outcome
