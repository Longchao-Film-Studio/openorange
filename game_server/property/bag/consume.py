"""扣道具（按 itemId 找格子）—— 供非 UseItem 入口复用（送礼 / 兑换 / 提交）。

UseItem_CS 自己的扣减在 item_use.apply 里（它按 ext.id 精确格子）；
这里的语义是"按 itemId 找第一个够数的格子"，用于客户端只报 itemId+count 的请求。
"""
from typing import Any

from shared.log_tee import get_logger

from ...write_guard import mutable as _mutable
from ..tx import Tx
from . import grants
from .errors import BAG_INVALID_ITEM_ID, BAG_INVALID_NUM, BagError

_log = get_logger(__name__)


def consume(props: dict[str, Any], item_id: int, count: int, *, tx: Tx) -> None:
    """扣 count 个 item_id；不足抛 BagError（调用方不落任何变更）。"""
    item_id = int(item_id)
    count = int(count)
    if count <= 0:
        raise BagError(BAG_INVALID_NUM, "数量必须为正")
    bag = grants.item_bag(props)
    for key, grid in list(bag.items()):
        if not isinstance(grid, dict):
            continue
        if int(grid.get("itemId") or 0) != item_id:
            continue
        num = int(grid.get("num") or 0)
        if num < count:
            raise BagError(BAG_INVALID_NUM, f"道具 {item_id} 数量不足（{num} < {count}）")
        if num == count:
            tx.delete(("item_bag", key))
        else:
            # 副本 -> Tx：直接改 props 里的 grid 是裸写（守卫会抛，连接被打断）
            new_grid = _mutable(grid)
            new_grid["num"] = num - count
            tx.set(("item_bag", key), new_grid)
        return
    raise BagError(BAG_INVALID_ITEM_ID, f"背包里没有道具 {item_id}")
