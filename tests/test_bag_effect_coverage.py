#!/usr/bin/env python
"""道具效果分类覆盖测试（INV-ITEM-6）。

数据驱动：遍历 ItemData 全表，断言**每一条可用道具**的 useType 都被明确分过类：

    实现（EFFECTS） / 计划中（PENDING） / 依赖未实现系统（UNSUPPORTED）

任何新 useType 出现而没登记 -> 测试红。这样「978 条道具 100% 有归属」
不靠人背，靠数据。

用法: python tests/test_bag_effect_coverage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server.property import bag  # noqa: E402,F401  (import 包 = 触发 handler 注册)
from game_server.property.bag import effects  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    table = gamedata.table("ItemData")
    items = [v for v in table.values() if isinstance(v, dict) and "itemId" in v]
    usable = [it for it in items if it.get("useType")]
    print(f"\n道具总数 {len(items)}，可用 {len(usable)}")

    known = set(effects.EFFECTS) | set(effects.PENDING_USE_TYPES) | set(effects.UNSUPPORTED_USE_TYPES)
    print("\n[1] 所有 useType 都必须登记")
    unclassified = sorted({it["useType"] for it in usable} - known)
    check("没有未登记的 useType", not unclassified, unclassified)
    check("实现/计划中/不支持 三张表互不重叠",
          not (set(effects.EFFECTS) & set(effects.PENDING_USE_TYPES))
          and not (set(effects.EFFECTS) & set(effects.UNSUPPORTED_USE_TYPES))
          and not (set(effects.PENDING_USE_TYPES) & set(effects.UNSUPPORTED_USE_TYPES)),
          (sorted(effects.EFFECTS), sorted(effects.PENDING_USE_TYPES), sorted(effects.UNSUPPORTED_USE_TYPES)))

    print("\n[2] 每条可用道具都落到某一类，且条数对得上")
    buckets = {"实现": [], "计划中": [], "不支持": []}
    for it in usable:
        ut = it["useType"]
        if ut in effects.EFFECTS:
            buckets["实现"].append(it)
        elif ut in effects.PENDING_USE_TYPES:
            buckets["计划中"].append(it)
        elif ut in effects.UNSUPPORTED_USE_TYPES:
            buckets["不支持"].append(it)
        else:
            buckets.setdefault("未分类", []).append(it)
    total = sum(len(v) for v in buckets.values())
    check("分类总数 = 可用道具数", total == len(usable), (total, len(usable)))
    check("没有未分类", not buckets.get("未分类"), len(buckets.get("未分类") or []))
    print(f"     实现 {len(buckets['实现'])} / 计划中 {len(buckets['计划中'])} / "
          f"不支持 {len(buckets['不支持'])}")

    print("\n[3] 三个分类的语义检查")
    check("实现类的 useType 都有 handler", all(
        effects.handler_for(ut) is not None for ut in effects.EFFECTS))
    check("不支持类都写了原因", all(bool(v) for v in effects.UNSUPPORTED_USE_TYPES.values()))
    check("计划中类都写了计划", all(bool(v) for v in effects.PENDING_USE_TYPES.values()))
    check("装饰映射覆盖 7 个 bag",
          len(__import__("game_server.property.bag.item_use", fromlist=["DECO_BAGS"]).DECO_BAGS) == 7)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
