#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从反编译的客户端配置表里抽取 ID / 名字索引。

用法:
    python tools/extract_tables.py --lua-dir <LuaAll_Decompiled2 的路径>
    python tools/extract_tables.py --lua-dir <...> --out shared/data

产物:
    shared/data/items.json   { id, name, type, quality }
    shared/data/chars.json   { id, name, name_en, title, grade, area, cv, show }

为什么要抽: 这两张表是客户端本地的 Lua 表（itemdata.lua 41k 行、chardata.lua 3.7k 行），
服务端做命令系统时需要按 ID 或名字查物品/角色。游戏已停更，表不会变，所以抽成 JSON
提交进仓库，运行时不再依赖客户端资源。

Lua 表里的中文字符串是十进制字节转义（"\233\149\191"），这里统一解码成 UTF-8。
"""
import argparse
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 匹配一条记录的开头：  [1001] = {
ENTRY_RE = re.compile(r"^\s*\[(\d+)\]\s*=\s*\{\s*$")

# 匹配完整的 Lua 字符串字面量（含转义），用于剔除后再数大括号
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')

SIMPLE_ESCAPES = {
    "n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11,
    "\\": 92, '"': 34, "'": 39, "0": 0,
}


def lua_unescape(s):
    """把 Lua 字符串里的十进制字节转义解码成 UTF-8 文本。"""
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt.isdigit():
                j = i + 1
                while j < n and s[j].isdigit() and (j - i) <= 3:
                    j += 1
                out.append(int(s[i + 1:j]) & 0xFF)
                i = j
                continue
            if nxt in SIMPLE_ESCAPES:
                out.append(SIMPLE_ESCAPES[nxt])
                i += 2
                continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_strings(line):
    """去掉字符串字面量，避免字符串里的括号干扰括号计数。"""
    return STRING_RE.sub('""', line)


def parse_entries(path):
    """逐条解析 Lua 表，返回 [(key, [body_lines])]。"""
    entries = []
    key = None
    depth = 0
    body = []
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip("\r")
            if key is None:
                m = ENTRY_RE.match(line)
                if m:
                    key = int(m.group(1))
                    depth = 1
                    body = []
                continue
            stripped = _strip_strings(line)
            depth += stripped.count("{") - stripped.count("}")
            body.append(line)
            if depth <= 0:
                entries.append((key, body))
                key = None
                depth = 0
                body = []
    return entries


def get_str(body, field):
    pat = re.compile(r"^\s*" + field + r"\s*=\s*\"((?:\\.|[^\"\\])*)\"\s*,?\s*$")
    for line in body:
        m = pat.match(line)
        if m:
            return lua_unescape(m.group(1))
    return None


def get_int(body, field):
    pat = re.compile(r"^\s*" + field + r"\s*=\s*(-?\d+)\s*,?\s*$")
    for line in body:
        m = pat.match(line)
        if m:
            return int(m.group(1))
    return None


def get_bool(body, field):
    pat = re.compile(r"^\s*" + field + r"\s*=\s*(true|false)\s*,?\s*$")
    for line in body:
        m = pat.match(line)
        if m:
            return m.group(1) == "true"
    return None


# ---------------------------------------------------------------- 可用性分级
#
# 同名条目可能对应「能用的」和「非正常能用的」两种东西，解析时优先取能用的。
# tier 越小越优先；同 tier 视为真歧义，返回候选让调用方决定。
#
# 角色: isShow=true 是可玩角色；6 位 ID（>=100000）是皮肤/形态变体。
# 物品: 客户端没有显式的可用标记，用 useType / itemType 推断：
#       useType="char" 是「给角色」的物品（最正规），itemType=1 是常规物品，
#       其余是头像 / 形象 / 装饰等附属物。

def char_tier(char_id, show):
    if show is True:
        return 0
    if int(char_id or 0) < 100000:
        return 1
    return 2


def item_tier(item_type, use_type):
    if use_type == "char":
        return 0
    if item_type == 1:
        return 1
    return 2


def extract_items(lua_dir):
    path = os.path.join(lua_dir, "tabledata", "commondata", "itemdata.lua")
    rows = []
    for key, body in parse_entries(path):
        item_id = get_int(body, "itemId")
        name = get_str(body, "name")
        if item_id is None or not name:
            continue
        item_type = get_int(body, "itemType")
        use_type = get_str(body, "useType")
        rows.append({
            "id": item_id,
            "name": name,
            "type": item_type,
            "quality": get_int(body, "quality"),
            "use_type": use_type,
            "tier": item_tier(item_type, use_type),
        })
    rows.sort(key=lambda r: r["id"])
    return {
        "version": 1,
        "source": "tabledata/commondata/itemdata.lua",
        "count": len(rows),
        "items": rows,
    }


def extract_chars(lua_dir):
    path = os.path.join(lua_dir, "tabledata", "commondata", "chardata.lua")
    rows = []
    for key, body in parse_entries(path):
        char_id = get_int(body, "charId")
        name = get_str(body, "charName")
        if char_id is None or not name:
            continue
        show = get_bool(body, "isShow")
        rows.append({
            "id": char_id,
            "name": name,
            "name_en": get_str(body, "charNameEN"),
            "title": get_str(body, "charTitle"),
            "grade": get_int(body, "charGrade"),
            "area": get_str(body, "charArea"),
            "cv": get_str(body, "charCV"),
            "show": show,
            "tier": char_tier(char_id, show),
        })
    rows.sort(key=lambda r: r["id"])
    return {
        "version": 1,
        "source": "tabledata/commondata/chardata.lua",
        "count": len(rows),
        "chars": rows,
    }


def write_json(obj, path):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser(description="抽取物品/角色名字索引")
    ap.add_argument("--lua-dir", required=True,
                    help="反编译 Lua 的根目录（含 tabledata/commondata）")
    ap.add_argument("--out", default=None,
                    help="输出目录，默认 <仓库根>/shared/data")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out or os.path.join(root, "shared", "data")
    os.makedirs(out_dir, exist_ok=True)

    lua_dir = os.path.abspath(args.lua_dir)
    if not os.path.isdir(lua_dir):
        print("找不到 Lua 目录:", lua_dir)
        return 2

    items = extract_items(lua_dir)
    chars = extract_chars(lua_dir)

    p1 = os.path.join(out_dir, "items.json")
    p2 = os.path.join(out_dir, "chars.json")
    s1 = write_json(items, p1)
    s2 = write_json(chars, p2)

    print("物品: %d 条 -> %s (%.1f KB)" % (items["count"], p1, s1 / 1024.0))
    print("角色: %d 条 -> %s (%.1f KB)" % (chars["count"], p2, s2 / 1024.0))

    # 抽样打印，方便肉眼核对
    print()
    print("物品抽样:")
    for it in items["items"][:5]:
        print("  %-7d %s" % (it["id"], it["name"]))
    print("角色抽样:")
    for ch in chars["chars"][:5]:
        print("  %-7d %-10s %s" % (ch["id"], ch["name"], ch["title"] or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
