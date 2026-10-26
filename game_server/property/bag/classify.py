"""useType 分类（**唯一来源**）：实现 / 计划中 / 显式拒绝。

覆盖测试和文档生成都调用这里，避免"测试里一套判断、文档里另一套"。
"""
from typing import Any

from ... import gamedata
from . import effects

IMPLEMENTED = "implemented"
PENDING = "pending"
UNSUPPORTED = "unsupported"

_LABELS = {IMPLEMENTED: "实现", PENDING: "计划中", UNSUPPORTED: "不支持"}


def label(kind: str) -> str:
    return _LABELS.get(kind, kind)


def classify(use_type: str | None) -> str:
    """单个 useType 的归类。None/空 -> 视为 unsupported（没有效果可执行）。"""
    if not use_type:
        return UNSUPPORTED
    if use_type in effects.EFFECTS:
        return IMPLEMENTED
    if use_type in effects.PENDING_USE_TYPES:
        return PENDING
    if use_type in effects.UNSUPPORTED_USE_TYPES:
        return UNSUPPORTED
    return "unregistered"


def usable_items() -> list[dict[str, Any]]:
    """ItemData 里带 useType 的行（= 有可用效果的道具）。"""
    return [v for v in gamedata.table("ItemData").values()
            if isinstance(v, dict) and v.get("useType")]


def summary() -> dict[str, int]:
    """{分类: 道具数}，另含 total。"""
    out: dict[str, int] = {}
    for it in usable_items():
        k = classify(it.get("useType"))
        out[k] = out.get(k, 0) + 1
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def unsupported_rows() -> list[dict[str, Any]]:
    """显式拒绝清单：[{use_type, reason, count, samples}]（按道具数降序）。"""
    by_type: dict[str, list[int]] = {}
    for it in usable_items():
        ut = it.get("useType")
        if classify(ut) == UNSUPPORTED and ut:
            by_type.setdefault(ut, []).append(int(it["itemId"]))
    rows: list[dict[str, Any]] = []
    for ut, ids in by_type.items():
        ids.sort()
        rows.append({"use_type": ut,
                     "reason": effects.UNSUPPORTED_USE_TYPES.get(ut, "（无 useType）"),
                     "count": len(ids),
                     "samples": ids[:5]})
    rows.sort(key=lambda r: (-int(r["count"]), str(r["use_type"])))
    return rows
