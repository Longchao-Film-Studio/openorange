#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""物品 / 角色索引测试。

用法:
    python tests/test_gamedata.py

只读 shared/data/*.json，不需要启动任何服务。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import gamedata as gd  # noqa: E402

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] 索引加载")
    st = gd.stats()
    check("物品 1866 条", st["items"] == 1866, st["items"])
    check("物品名字 1596 个", st["item_names"] == 1596, st["item_names"])
    check("角色 105 条", st["chars"] == 105, st["chars"])
    check("角色名字 56 个", st["char_names"] == 56, st["char_names"])

    print("\n[2] 物品按 ID 解析")
    e, cands = gd.resolve_item("1001")
    check("1001 命中", e is not None, cands)
    check("1001 名字是 长命锁", e and e["name"] == "长命锁", e)
    check("按 ID 不会有候选", cands == [], cands)
    e, _ = gd.resolve_item("#1001")
    check("#1001 同样命中", e and e["id"] == 1001, e)

    print("\n[3] 物品按唯一名字解析")
    e, cands = gd.resolve_item("龙玉")
    check("龙玉 唯一命中", e is not None and cands == [], (e, cands))
    check("龙玉 ID 是 101", e and e["id"] == 101, e)

    print("\n[4] 同名条目按「可用性」优先")
    # 「长命锁」有 3 条：1001 是给角色的物品（tier0），71118/75118 是头像/形象（tier2）
    e, cands = gd.resolve_item("长命锁")
    check("长命锁 命中可用的 1001", e is not None and e["id"] == 1001, (e, cands))
    check("长命锁 不返回候选", cands == [], cands)
    check("命中的是角色物品", gd.tier_name(e) == "角色物品", gd.tier_name(e))

    print("\n[4b] --allow-unavailables 取消优先规则")
    e, cands = gd.resolve_item("长命锁", allow_unavailables=True)
    check("开启后不再自动命中", e is None, e)
    check("开启后返回 3 个候选", len(cands) == 3, [c["id"] for c in cands])
    check("候选 ID 正确", sorted(c["id"] for c in cands) == [1001, 71118, 75118],
          [c["id"] for c in cands])

    print("\n[4c] 同档重名仍然报歧义")
    # 「随机龙咒」4 条同为 tier2，没有可优先的，必须如实返回候选
    e, cands = gd.resolve_item("随机龙咒")
    check("随机龙咒 不直接命中", e is None, e)
    check("随机龙咒 返回 4 个候选", len(cands) == 4, [c["id"] for c in cands])
    check("候选 ID 正确", sorted(c["id"] for c in cands) == [5004, 5005, 5006, 5007],
          [c["id"] for c in cands])

    print("\n[5] 找不到")
    e, cands = gd.resolve_item("这个物品不存在")
    check("未找到返回空", e is None and cands == [], (e, cands))
    e, cands = gd.resolve_item("")
    check("空字符串返回空", e is None and cands == [], (e, cands))
    e, cands = gd.resolve_item("#abc")
    check("# 后面不是数字返回空", e is None and cands == [], (e, cands))

    print("\n[6] 角色解析优先级")
    e, cands = gd.resolve_char("长命锁")
    check("长命锁 命中真角色 1001", e is not None and e["id"] == 1001, (e, cands))
    check("不是皮肤变体 100101", not (e and e["id"] == 100101), e)
    e, _ = gd.resolve_char("拂云")
    check("拂云 命中基础角色 1013", e is not None and e["id"] == 1013, e)
    e, _ = gd.resolve_char("星牧")
    check("星牧 命中 1006", e is not None and e["id"] == 1006, e)

    print("\n[7] 角色真歧义")
    e, cands = gd.resolve_char("瞳")
    check("瞳 不直接命中", e is None, e)
    check("瞳 返回 2 个候选", len(cands) == 2, [c["id"] for c in cands])
    check("瞳 候选是 9001/9002", sorted(c["id"] for c in cands) == [9001, 9002],
          [c["id"] for c in cands])

    print("\n[7b] 角色 --allow-unavailables")
    e, cands = gd.resolve_char("长命锁", allow_unavailables=True)
    check("开启后长命锁不再自动命中", e is None, e)
    check("开启后返回 3 个候选", len(cands) == 3, [c["id"] for c in cands])
    check("候选含皮肤变体", 100101 in [c["id"] for c in cands], [c["id"] for c in cands])

    print("\n[8] 强制 ID 绕过名字")
    e, cands = gd.resolve_char("#101301")
    check("#101301 命中变体", e is not None and e["id"] == 101301, e)
    check("强制 ID 无候选", cands == [], cands)

    print("\n[9] 模糊搜索")
    hits = gd.search_items("长命")
    check("搜 长命 有结果", len(hits) >= 3, len(hits))
    check("精确匹配排第一", hits[0]["name"] == "长命锁" or hits[0]["name"].startswith("长命锁"),
          hits[0]["name"] if hits else None)
    ids = [h["id"] for h in hits]
    check("搜到 1001", 1001 in ids, ids[:10])
    check("搜到 71118", 71118 in ids, ids[:10])
    hits = gd.search_items("不存在的关键字")
    check("搜不到返回空", hits == [], hits)

    print("\n[10] 英文名搜索")
    hits = gd.search_chars("CHANG")
    check("按英文名能搜到角色", len(hits) >= 1, len(hits))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
