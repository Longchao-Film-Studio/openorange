#!/usr/bin/env python3
"""tools/gen_contract.py —— 从客户端 generated/config.lua 生成回调契约表。

为什么必须有这个工具
--------------------
docs/P1-剧情任务状态机.md §2.1 的「属性 -> 回调 -> optype」表以前是手抄进文档的。
按反补丁驱动 R5（文档数字必须现场生成），手抄的表会过期 ——
本次开发过程中它就错过两次（story_finished 漏了 entryAdded 行、
task_bag.*.* 的 optype 写反）。

这个脚本按行解析 generated/config.lua 里各 Component 的 PropertyCallbacks，
输出 tests/contract/property_callbacks.json。

optype 映射依据 core/common/propertyparser.lua:8-14：
    changed=OP_CHANGE(1)  entryAdded=OP_ADD(2)  entryDeleted=OP_DEL(3)
    itemInserted=OP_INSERT(4)  itemRemoved=OP_REMOVE(5)

用法:
    python tools/gen_contract.py            # 生成并打印摘要
    python tools/gen_contract.py --check    # 只校验现有 json 是否过期
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_LUA = os.environ.get(
    "OO_LUA_DIR",
    os.path.join(ROOT, "..", "archive", "game_data", "LuaAll_Decompiled2",
                 "generated", "config.lua"))
OUT = os.path.join(ROOT, "tests", "contract", "property_callbacks.json")

SECTION_OPTYPE = {
    "changed": 1,
    "entryAdded": 2,
    "entryDeleted": 3,
    "itemInserted": 4,
    "itemRemoved": 5,
}

COMPONENTS = [
    "AvatarStoryTaskComp",
    "AvatarStorylineComp",
    "AvatarTaskComp",
    "AvatarTroupeActComp",
]

# 形如：  ["story_task_bag.*"] = {      或      street_trace_taskid = {
RE_KEY = re.compile(r'^\s*(?:\[\s*"([^"]+)"\s*\]|([A-Za-z_][A-Za-z0-9_]*))\s*=\s*\{\s*$')
# 形如：      "onStoryTaskItemChanged"
RE_CB = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,?\s*$')


def parse_config():
    if not os.path.exists(CONFIG_LUA):
        print("[gen_contract] 找不到 {}".format(CONFIG_LUA), file=sys.stderr)
        return {}
    with open(CONFIG_LUA, encoding="utf-8") as f:
        lines = f.read().split("\n")

    out = {}
    i = 0
    n = len(lines)
    while i < n:
        m = re.match(r"^\s*({})\s*=\s*\{{\s*$".format("|".join(COMPONENTS)), lines[i])
        if not m:
            i += 1
            continue
        comp = m.group(1)
        # 找到该 Component 的 PropertyCallbacks 段
        j = i + 1
        while j < n and "PropertyCallbacks" not in lines[j]:
            j += 1
        if j >= n:
            break
        rows = []
        section = None
        prop = None
        seen_type = False
        # 从 PropertyCallbacks 那一行开始按花括号配平，走到它结束为止
        depth = 0
        started = False
        k = j
        while k < n:
            line = lines[k]
            depth += line.count("{") - line.count("}")
            if not started:
                started = depth > 0
                k += 1
                continue
            if depth <= 0:
                k += 1
                break
            sm = re.match(r"^\s*(changed|entryAdded|entryDeleted|itemInserted"
                          r"|itemRemoved)\s*=\s*\{\s*$", line)
            if sm:
                section = sm.group(1)
                prop = None
                k += 1
                continue
            km = RE_KEY.match(line)
            if km and section:
                prop = km.group(1) or km.group(2)
                seen_type = False
                k += 1
                continue
            cm = RE_CB.match(line)
            if cm and section and prop:
                if not seen_type:
                    # 每个条目第一行是类型，第二行才是回调名
                    seen_type = True
                    k += 1
                    continue
                rows.append({
                    "component": comp,
                    "property": prop,
                    "section": section,
                    "optype": SECTION_OPTYPE[section],
                    "callback": cm.group(1),
                })
                prop = None
                k += 1
                continue
            k += 1
        out[comp] = rows
        i = k
    return out


def flatten(parsed):
    flat = {}
    for rows in parsed.values():
        for r in rows:
            flat.setdefault(r["property"], []).append(r)
    return flat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    parsed = parse_config()
    if not parsed:
        print("[gen_contract] 没解析到任何 Component", file=sys.stderr)
        return 2
    flat = flatten(parsed)
    doc = {
        "_source": os.path.relpath(CONFIG_LUA, ROOT).replace("\\\\", "/"),
        "_optype_map": {"changed": 1, "entryAdded": 2, "entryDeleted": 3,
                        "itemInserted": 4, "itemRemoved": 5},
        "_note": "由 tools/gen_contract.py 从客户端 config.lua 生成，勿手改",
        "properties": dict(sorted(flat.items())),
    }

    if args.check:
        if not os.path.exists(OUT):
            print("[gen_contract] {} 不存在".format(OUT), file=sys.stderr)
            return 1
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("properties") == doc["properties"]:
            print('[gen_contract] 契约表和 config.lua 一致（{} 个属性）'.format(len(doc["properties"])))
            return 0
        print("[gen_contract] 契约表已过期，请重新生成", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print('[gen_contract] 已生成 {}（{} 个属性，来自 {}）'.format(os.path.relpath(OUT, ROOT), len(doc["properties"]), doc["_source"]))
    for k in sorted(doc["properties"]):
        rows = doc["properties"][k]
        desc = "; ".join("{}->{}(op={})".format(r["callback"], r["section"],
                                           r["optype"]) for r in rows)
        print('  {:<42} {}'.format(k, desc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
