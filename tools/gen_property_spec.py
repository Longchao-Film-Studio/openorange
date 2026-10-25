#!/usr/bin/env python3
"""生成 game_server/property/spec_data.json —— 属性声明表（P0 地基 B1）。

数据来源：客户端 generated/config.lua（**权威声明表**）

  Custom = { <TypeName> = { Properties = {...}, __ValueType__, __IntTypeKey__ } }
  AvatarProperties = { Properties = { <propName> = {type, default, aoiscope, persist} } }

为什么要生成而不是手写：
  属性名、类型、键类型、默认值全部在客户端有权威声明。
  之前服务端只用了生成期快照 avatar_init.json，属性名/类型判断全靠手写特例
  （REQUIRED / SIMPLE_PROPS 那 45 条注释），加一个字段要改两处。

输出结构：
  {
    "generated_from": "<config.lua 路径>",
    "custom_types": {
      "<TypeName>": {
        "props": {"<field>": ["<type>", "<default>"], ...},   # 结构体字段
        "value_type": "<TypeName>" | null,                    # 字典/列表的元素类型
        "int_key": true/false                                 # 字典的键是否整数
      }
    },
    "avatar_properties": {
      "<propName>": {"type": "...", "default": ..., "scope": "...", "persist": "..."}
    }
  }

用法：
    python tools/gen_property_spec.py            # 生成
    python tools/gen_property_spec.py --check    # 只统计不写
"""
import argparse
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
LUA_SRC = os.environ.get("OO_LUA_SRC") or r"F:\haowancheng\archive\game_data\LuaFull\src"
CONFIG_LUA = os.path.join(LUA_SRC, "generated", "config.lua")
OUT_JSON = os.path.join(_ROOT, "game_server", "property", "spec_data.json")


# ---------------------------------------------------------------- Lua 解析
def extract_block(s, start):
    """从 s[start] 的 '{' 开始配平，返回整块（含花括号）。"""
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def split_top(inner):
    """按顶层逗号切分（忽略嵌套括号内的逗号）。"""
    parts, depth, cur = [], 0, ""
    for c in inner:
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur.strip())
    return parts


def lua_value(tok):
    """把一个 Lua 字面量转成 Python 值。只处理 config.lua 里出现的子集。"""
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if tok == "true":
        return True
    if tok == "false":
        return False
    if tok == "nil":
        return None
    if tok.startswith("{"):
        return {}          # 默认值只关心「是不是表」，不展开
    try:
        if re.match(r"^-?\d+$", tok):
            return int(tok)
        return float(tok)
    except ValueError:
        return tok


def parse_custom_types(txt):
    """解析 Custom 段：name -> {props, value_type, int_key}。"""
    i = txt.find("  Custom = {")
    if i < 0:
        raise RuntimeError("config.lua 里找不到 Custom 段")
    cblk = extract_block(txt, txt.find("{", i))
    types = {}
    for m in re.finditer(r"^    (\w+) = \{", cblk, re.MULTILINE):
        name = m.group(1)
        blk = extract_block(cblk, cblk.find("{", m.start()))
        props = {}
        pm = blk.find("Properties = {")
        if pm >= 0:
            pb = extract_block(blk, blk.find("{", pm))
            for pm2 in re.finditer(r"^        (\w+) = \{", pb, re.MULTILINE):
                k = pm2.group(1)
                eb = extract_block(pb, pb.find("{", pm2.start()))
                parts = split_top(eb[1:-1])
                typ = parts[0].strip().strip('"') if parts else "?"
                dflt = lua_value(parts[1]) if len(parts) > 1 else None
                props[k] = [typ, dflt]
        vm = re.search(r'__ValueType__\s*=\s*"([^"]+)"', blk)
        im = re.search(r"__IntTypeKey__\s*=\s*(\w+)", blk)
        types[name] = {
            "props": props,
            "value_type": vm.group(1) if vm else None,
            "int_key": bool(im and im.group(1) == "true"),
        }
    return types


def parse_container_kinds():
    """从客户端 gamecommon/customtypes/*.lua 抽「类型名 -> list/dict」。

    ★ 为什么需要：config.lua 里 IntList 和 GachaRecord 的声明**长得一模一样**
      （都是 __ValueType__ + __IntTypeKey__），光看 config.lua 区分不出
      容器是 list 还是 dict。真正的区分在客户端类定义里：
        class.Class("IntList", CustomList)   -- list
        class.Class("ItemBag", CustomDict)   -- dict

    审计实测：按「类型名以 List 结尾」猜，342 个类型里错 7 个
      （ActivityCheckinRecords/GachaRecord/GachaRewards/Signs 是 list 但名字不带 List；
       ChampionshipScheduleInfoList/CharShownList/EndlessRankInfoRecordList 是 dict 但名字带 List）。

    返回 {type_name: "list" | "dict"}。
    """
    out = {}
    if not os.path.isdir(LUA_SRC):
        return out
    # class.Class("Name", CustomList|CustomDict)，前面可能带 local X =
    pat = re.compile(
        r'class\.Class\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,\s*(CustomList|CustomDict)\s*\)')
    for dirpath, _dirs, files in os.walk(LUA_SRC):
        for fn in files:
            if not fn.endswith(".lua"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if "class.Class" not in txt:
                continue
            for m in pat.finditer(txt):
                out[m.group(1)] = ("list" if m.group(2) == "CustomList"
                                  else "dict")
    return out


def parse_avatar_properties(txt):
    """解析 AvatarProperties.Properties：name -> {type, default, scope, persist}。"""
    i = txt.find("    AvatarProperties = {")
    if i < 0:
        raise RuntimeError("config.lua 里找不到 AvatarProperties 段")
    blk = extract_block(txt, txt.find("{", i))
    pm = blk.find("Properties = {")
    if pm < 0:
        raise RuntimeError("AvatarProperties 里没有 Properties")
    pb = extract_block(blk, blk.find("{", pm))
    out = {}
    for m in re.finditer(r"^        (\w+) = \{", pb, re.MULTILINE):
        name = m.group(1)
        eb = extract_block(pb, pb.find("{", m.start()))
        parts = split_top(eb[1:-1])
        out[name] = {
            "type": parts[0].strip().strip('"') if parts else "?",
            "default": lua_value(parts[1]) if len(parts) > 1 else None,
            "scope": parts[2].strip().strip('"') if len(parts) > 2 else "",
            "persist": parts[3].strip().strip('"') if len(parts) > 3 else "",
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只统计不写文件")
    args = ap.parse_args()

    if not os.path.exists(CONFIG_LUA):
        raise SystemExit(f"找不到 config.lua: {CONFIG_LUA}")
    txt = open(CONFIG_LUA, encoding="utf-8", errors="replace").read()

    types = parse_custom_types(txt)
    props = parse_avatar_properties(txt)
    kinds = parse_container_kinds()
    # 把权威的 list/dict 判定塞进每个自定义类型
    hit = 0
    for name, info in types.items():
        if info.get("value_type"):
            k = kinds.get(name)
            info["container_kind"] = k
            if k:
                hit += 1
    print('  容器类型权威判定: {} / {}（其余未在客户端类定义里找到）'.format(hit, sum(1 for t in types.values() if t.get("value_type"))))

    structs = sum(1 for t in types.values() if t["props"])
    dicts = sum(1 for t in types.values()
                if not t["props"] and t["value_type"])
    print('config.lua: {} 行'.format(txt.count("\n")))
    print(f'  自定义类型 {len(types)}（结构体 {structs} / 字典或列表 {dicts} / 其它 {len(types) - structs - dicts}）')
    print(f'  AvatarProperties 属性 {len(props)}')

    simple = [p for p in props.values()
              if p["type"] in ("int", "double", "string", "boolean")]
    print(f'    简单类型 {len(simple)} / 自定义类型 {len(props) - len(simple)}')

    # 自检：属性里引用的自定义类型必须都在 Custom 里
    missing = sorted({p["type"] for p in props.values()
                      if p["type"] not in ("int", "double", "string", "boolean")
                      and p["type"] not in types})
    if missing:
        print(f"  ⚠ 属性引用了但 Custom 里没有的类型: {missing}")
    else:
        print("  ✅ 属性引用的自定义类型全部存在")

    # 自检：结构体字段引用的类型也必须存在
    bad_fields = []
    for tn, t in types.items():
        for f, (ft, _dv) in t["props"].items():
            if ft not in ("int", "double", "string", "boolean") and ft not in types:
                bad_fields.append(f"{tn}.{f} -> {ft}")
    if bad_fields:
        print(f"  ⚠ 结构体字段引用了不存在的类型: {bad_fields[:8]}")
    else:
        print("  ✅ 结构体字段类型全部存在")

    data = {
        "generated_from": CONFIG_LUA,
        "custom_types": types,
        "avatar_properties": props,
    }

    if args.check:
        # ★ 审计 P1-6：--check 必须做 diff，否则客户端版本更新后
        #   spec_data.json 会静默变旧，Tx 的 struct/container 判断跟着错。
        if not os.path.exists(OUT_JSON):
            print(f"❌ {OUT_JSON} 不存在")
            return 1
        with open(OUT_JSON, "r", encoding="utf-8") as f:
            old = json.load(f)
        diffs = []
        for key in ("custom_types", "avatar_properties"):
            a = old.get(key) or {}
            b = data.get(key) or {}
            if a == b:
                continue
            only_a = sorted(set(a) - set(b))
            only_b = sorted(set(b) - set(a))
            changed = sorted(k for k in (set(a) & set(b)) if a[k] != b[k])
            if only_a:
                diffs.append(f'  {key} 少了 {len(only_a)} 项: {only_a[:5]}')
            if only_b:
                diffs.append(f'  {key} 多了 {len(only_b)} 项: {only_b[:5]}')
            if changed:
                diffs.append(f'  {key} 变了 {len(changed)} 项: {changed[:5]}')
        if diffs:
            print("❌ spec_data.json 已过期，请重新生成：")
            for d in diffs:
                print(d)
            return 1
        print("✅ spec_data.json 与 config.lua 一致")
        return 0

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"写出 {OUT_JSON}（{os.path.getsize(OUT_JSON) / 1024.0:.1f} KB）")


if __name__ == "__main__":
    sys.exit(main() or 0)
