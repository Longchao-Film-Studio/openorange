#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared.gamedata —— 物品 / 角色 索引

数据来源: tools/extract_tables.py 从客户端配置表抽取的 JSON。
游戏已停更，表不会变，所以直接提交进仓库，运行时不依赖客户端资源。

核心接口:
    resolve_item(token)   解析物品（ID 或名字），重名时返回候选
    resolve_char(token)   解析角色（ID 或名字）
    search(kind, kw)      模糊搜索

token 规则:
    "1001"    纯数字      -> 按 ID
    "#1001"   井号开头    -> 强制按 ID（跳过名字查找）
    "长命锁"  其它        -> 按名字（唯一则命中，重名返回候选）
"""
import io
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

_items = None
_chars = None
_item_by_id = {}
_item_by_name = {}
_char_by_id = {}
_char_by_name = {}


def _load(name, list_key):
    path = os.path.join(DATA_DIR, name)
    with io.open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get(list_key) or []


def _ensure_loaded():
    global _items, _chars
    if _items is not None:
        return
    _items = _load("items.json", "items")
    _chars = _load("chars.json", "chars")
    for it in _items:
        _item_by_id[it["id"]] = it
        _item_by_name.setdefault(it["name"], []).append(it)
    for ch in _chars:
        _char_by_id[ch["id"]] = ch
        _char_by_name.setdefault(ch["name"], []).append(ch)


def stats():
    _ensure_loaded()
    return {
        "items": len(_items),
        "item_names": len(_item_by_name),
        "chars": len(_chars),
        "char_names": len(_char_by_name),
    }


def _resolve(token, by_id, by_name, rank=None):
    """通用解析。返回 (entry, candidates)。

    rank: 重名时按该函数排序取最优（用于角色表区分真角色与皮肤变体）。
          返回可比较的元组，越小越优先；若前两名同分则视为真歧义，返回全部候选。
    """
    _ensure_loaded()
    token = str(token or "").strip()
    if not token:
        return None, []

    # 井号前缀：强制按 ID
    force_id = token.startswith("#")
    if force_id:
        token = token[1:].strip()

    if force_id or token.isdigit():
        if not token.isdigit():
            return None, []
        entry = by_id.get(int(token))
        return (entry, []) if entry else (None, [])

    # 按名字
    hits = by_name.get(token) or []
    if len(hits) > 1 and rank is not None:
        ranked = sorted(hits, key=rank)
        if rank(ranked[0]) < rank(ranked[1]):
            return ranked[0], []
        best = rank(ranked[0])
        hits = [h for h in ranked if rank(h) == best]
    if len(hits) == 1:
        return hits[0], []
    if len(hits) > 1:
        return None, list(hits)
    return None, []


def resolve_item(token):
    """解析物品。返回 (entry, candidates)。"""
    return _resolve(token, _item_by_id, _item_by_name)


def _char_rank(ch):
    """角色重名时的优先级。

    角色表 105 条里只有 20 条是真·可玩角色，其余是娃娃形态（2001+）与皮肤变体（100101+）。
    排序键: 先看客户端自己的 isShow 标记，再看 ID 是否为基础角色（6 位 ID 是变体）。
    这样「长命锁」命中 1001，「拂云」命中 1013，而「瞳」因为 9001/9002 同为真角色
    会如实返回两个候选。
    """
    return (0 if ch.get("show") is True else 1,
            0 if int(ch.get("id") or 0) < 100000 else 1)


def resolve_char(token):
    """解析角色。返回 (entry, candidates)。"""
    return _resolve(token, _char_by_id, _char_by_name, rank=_char_rank)


def _search(rows, keyword):
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    low = keyword.lower()
    exact, prefix, contain = [], [], []
    for r in rows:
        name = r.get("name") or ""
        name_en = (r.get("name_en") or "").lower()
        if name == keyword or name_en == low:
            exact.append(r)
        elif name.startswith(keyword) or name_en.startswith(low):
            prefix.append(r)
        elif keyword in name or (name_en and low in name_en):
            contain.append(r)
    return exact + prefix + contain


def search_items(keyword):
    _ensure_loaded()
    return _search(_items, keyword)


def search_chars(keyword):
    _ensure_loaded()
    return _search(_chars, keyword)


def item_count_by_name(name):
    """某个名字对应几条记录（用于判断是否重名）。"""
    _ensure_loaded()
    return len(_item_by_name.get(str(name or ""), []))


def char_count_by_name(name):
    _ensure_loaded()
    return len(_char_by_name.get(str(name or ""), []))


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(stats())
    for tok in ("1001", "#1001", "长命锁", "龙玉", "不存在的名字"):
        e, cands = resolve_item(tok)
        if e:
            print("%-10s -> %s" % (tok, e["name"]))
        elif cands:
            print("%-10s -> 重名 %d 条: %s" % (tok, len(cands), [c["id"] for c in cands]))
        else:
            print("%-10s -> 未找到" % tok)
    print()
    for r in search_items("长命")[:6]:
        print("  search 长命:", r["id"], r["name"])
