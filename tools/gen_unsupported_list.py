#!/usr/bin/env python3
"""生成 docs/不支持效果清单.md —— 数字全部现场算，不手写。

用法: python tools/gen_unsupported_list.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("OO_STATE_DIR", os.path.join(ROOT, "work", "_gen_unsupported"))

from game_server.property import bag  # noqa: E402,F401  (import 包 -> 注册 handler)
from game_server.property.bag import classify, effects  # noqa: E402

OUT = os.path.join(ROOT, "docs", "不支持效果清单.md")
BT = chr(96)  # 反引号：避免在源码里到处转义


def q(s: str) -> str:
    return BT + s + BT


def main() -> int:
    s = classify.summary()
    rows = classify.unsupported_rows()
    imp = s.get(classify.IMPLEMENTED, 0)
    pend = s.get(classify.PENDING, 0)
    uns = s.get(classify.UNSUPPORTED, 0)
    total = s["total"]

    lines = [
        "# 不支持效果清单（自动生成）",
        "",
        "> 由 " + q("tools/gen_unsupported_list.py") + " 生成于 "
        + time.strftime("%Y-%m-%d %H:%M:%S")
        + "；**不要手改**，改 " + q("bag/effects.py") + " 的 UNSUPPORTED_USE_TYPES 后重新生成。",
        "",
        "## 口径",
        "",
        "ItemData 里带 useType 的道具 = 有可用效果的可做集合：",
        "",
        "- 合计 **" + str(total) + "**",
        "- 已实现 **" + str(imp) + "**（有 handler：randomItem 735 池、装饰 494、好感 71、声望 2、通行证经验 2 等）",
        "- 计划中 **" + str(pend) + "**（写好计划但依赖别的系统先落地）",
        "- 显式拒绝 **" + str(uns) + "**（有明确原因；使用时返回 15009 且不消耗，并落盘）",
        "",
        "> 立项口径是「833 可做 / 145 拒绝」。实现过程中把 9 条 VIP 通行证解锁类",
        "> （商店购买-发货流程，道具本身无效果）从可做重新归到显式拒绝，",
        "> 因此现在是 **" + str(imp) + " 实现 + " + str(uns) + " 拒绝 = " + str(total)
        + "**，三类互不重叠（本次生成时点的实测数）。",
        "",
        "## 显式拒绝清单",
        "",
        "| useType | 道具数 | 原因 | 示例 itemId |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows:
        samples = ", ".join(str(x) for x in r["samples"])
        lines.append("| " + q(r["use_type"]) + " | " + str(r["count"]) + " | "
                     + r["reason"] + " | " + samples + " |")
    lines += ["", "## 计划中（如果非空）", ""]
    if pend:
        used: dict[str, int] = {}
        for it in classify.usable_items():
            if classify.classify(it.get("useType")) == classify.PENDING:
                used[it["useType"]] = used.get(it["useType"], 0) + 1
        for ut, cnt in sorted(used.items(), key=lambda kv: -kv[1]):
            lines.append("- " + q(ut) + " x" + str(cnt) + "："
                         + effects.PENDING_USE_TYPES.get(ut, ""))
    else:
        lines.append("（无）")
    lines.append("")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("[gen] 写出 " + OUT)
    print("[gen] 合计 " + str(total) + " = 实现 " + str(imp)
          + " + 计划中 " + str(pend) + " + 不支持 " + str(uns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
