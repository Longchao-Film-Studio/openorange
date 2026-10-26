#!/usr/bin/env python
"""道具效果分类覆盖测试（INV-ITEM-6）。

数据驱动：遍历 ItemData 全表，断言**每一条可用道具**的 useType 都被明确分过类：

    实现（EFFECTS） / 计划中（PENDING） / 依赖未实现系统（UNSUPPORTED）

分类判断只在 game_server.property.bag.classify 里写一份，本测试和
tools/gen_unsupported_list.py 都调它 —— 避免"测试一套、文档另一套"。

另含：
  · docs/不支持效果清单.md 必须与现场算出的数字一致（防止手改文档）
  · work/logs/unknown_item_effects.tsv 跨进程去重（重启不重复刷）

用法: python tests/test_bag_effect_coverage.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_cov_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server.property import bag  # noqa: E402,F401  (import 包 = 触发 handler 注册)
from game_server.property.bag import classify, effects, report  # noqa: E402

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

    print("\n[1] 所有 useType 都必须登记")
    kinds = {classify.classify(it["useType"]) for it in usable}
    check("没有未登记的 useType", "unregistered" not in kinds,
          sorted({it["useType"] for it in usable
                  if classify.classify(it["useType"]) == "unregistered"}))
    check("实现/计划中/不支持 三张表互不重叠",
          not (set(effects.EFFECTS) & set(effects.PENDING_USE_TYPES))
          and not (set(effects.EFFECTS) & set(effects.UNSUPPORTED_USE_TYPES))
          and not (set(effects.PENDING_USE_TYPES) & set(effects.UNSUPPORTED_USE_TYPES)),
          (sorted(effects.EFFECTS), sorted(effects.PENDING_USE_TYPES),
           sorted(effects.UNSUPPORTED_USE_TYPES)))

    print("\n[2] 每条可用道具都落到某一类，条数对得上")
    s = classify.summary()
    imp, pend, uns = (s.get(classify.IMPLEMENTED, 0), s.get(classify.PENDING, 0),
                      s.get(classify.UNSUPPORTED, 0))
    check("分类总数 = 可用道具数", s["total"] == len(usable), (s["total"], len(usable)))
    check("三类相加 = 总数", imp + pend + uns == s["total"], (imp, pend, uns, s["total"]))
    print(f"     实现 {imp} / 计划中 {pend} / 不支持 {uns}（合计 {s['total']}）")

    print("\n[3] 三个分类的语义检查")
    check("实现类的 useType 都有 handler", all(
        effects.handler_for(ut) is not None for ut in effects.EFFECTS))
    check("不支持类都写了原因", all(bool(v) for v in effects.UNSUPPORTED_USE_TYPES.values()))
    check("计划中类都写了计划", all(bool(v) for v in effects.PENDING_USE_TYPES.values()))
    check("装饰映射覆盖 7 个 bag",
          len(__import__("game_server.property.bag.item_use", fromlist=["DECO_BAGS"]).DECO_BAGS) == 7)
    rows = classify.unsupported_rows()
    check("显式拒绝清单条目数与分类数一致", sum(r["count"] for r in rows) == uns,
          (sum(r["count"] for r in rows), uns))

    print("\n[4] 文档数字必须现场一致（防手改）")
    doc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "不支持效果清单.md")
    if os.path.exists(doc):
        text = open(doc, encoding="utf-8").read()
        check(f"合计 {s['total']}", f"合计 **{s['total']}**" in text)
        check(f"已实现 {imp}", f"已实现 **{imp}**" in text)
        check(f"显式拒绝 {uns}", f"显式拒绝 **{uns}**" in text)
    else:
        check("清单文档存在", False, doc)

    print("\n[5] 落盘清单跨进程去重")
    d = tempfile.mkdtemp(prefix="oo_tsv_")
    p = os.path.join(d, "unknown_item_effects.tsv")
    from game_server import config
    old_dir = config.LOG_DIR
    config.LOG_DIR = d
    report.reset_for_tests()
    try:
        report.note("use_type", "charSkin", "皮肤系统未实现")
        report.note("use_type", "charSkin", "重复调用不应再写")
        lines = [x for x in open(p, encoding="utf-8").read().split("\n") if x.strip()]
        check("表头只有 1 行", sum(1 for x in lines if x.startswith("kind\tkey\t")) == 1, lines[:3])
        check("同键只写 1 条", len(lines) == 2, lines)
        report.reset_for_tests()          # 模拟进程重启（内存清空，文件保留）
        report.note("use_type", "charSkin", "重启后也不应重复写")
        lines2 = [x for x in open(p, encoding="utf-8").read().split("\n") if x.strip()]
        check("重启后仍只 1 条（跨进程去重）", len(lines2) == 2, lines2)
    finally:
        config.LOG_DIR = old_dir
        report.reset_for_tests()

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
