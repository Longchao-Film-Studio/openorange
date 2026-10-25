#!/usr/bin/env python3
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
import json
import os

from shared.log_tee import get_logger

_log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

_items = None
_chars = None
item_by_id = {}
_item_by_name = {}
_char_by_id = {}
_char_by_name = {}

# 命令系统用到的附加表（同样是 BDD 来的真数据）
currencies = None
activities = None
_tasks = None
_maps = None
_gachas = None
_stories = None
_currency_by_id = {}
_activity_by_id = {}
_activity_by_name = {}
_task_by_id = {}
_map_by_id = {}
_map_by_name = {}
_gacha_by_id = {}
_gacha_by_name = {}
_story_by_id = {}


def _load(name, list_key):
    path = os.path.join(DATA_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get(list_key) or []


def _load_optional(name, list_key):
    """附加表缺失时返回空列表，不影响主流程。"""
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get(list_key) or []


def _ensure_loaded():
    global _items, _chars, currencies, activities, _tasks, _maps, _gachas, _stories
    if _items is not None:
        return
    _items = _load("items.json", "items")
    _chars = _load("chars.json", "chars")
    for it in _items:
        item_by_id[it["id"]] = it
        _item_by_name.setdefault(it["name"], []).append(it)
    for ch in _chars:
        _char_by_id[ch["id"]] = ch
        _char_by_name.setdefault(ch["name"], []).append(ch)

    currencies = _load_optional("currencies.json", "currencies")
    activities = _load_optional("activities.json", "activities")
    _tasks = _load_optional("tasks.json", "tasks")
    _maps = _load_optional("maps.json", "maps")
    _gachas = _load_optional("gachas.json", "gachas")
    _stories = _load_optional("stories.json", "stories")
    for c in currencies:
        _currency_by_id[c["id"]] = c
    for a in activities:
        _activity_by_id[a["id"]] = a
        if a.get("name"):
            _activity_by_name.setdefault(a["name"], []).append(a)
    for t in _tasks:
        _task_by_id[t["id"]] = t
    for m in _maps:
        _map_by_id[m["id"]] = m
        if m.get("name"):
            _map_by_name.setdefault(m["name"], []).append(m)
    for g in _gachas:
        _gacha_by_id[g["id"]] = g
        if g.get("name"):
            _gacha_by_name.setdefault(g["name"], []).append(g)
    for s in _stories:
        _story_by_id[s["id"]] = s


def stats():
    _ensure_loaded()
    return {
        "items": len(_items),
        "item_names": len(_item_by_name),
        "chars": len(_chars),
        "char_names": len(_char_by_name),
        "playable_chars": sum(1 for c in _chars if _tier(c) == 0),
        "currencies": len(currencies),
        "activities": len(activities),
        "tasks": len(_tasks),
        "maps": len(_maps),
        "gachas": len(_gachas),
        "stories": len(_stories),
    }


def _resolve(token, by_id, by_name, allow_unavailables=False):
    """通用解析。返回 (entry, candidates)。

    重名时的默认规则：只保留 tier 最小的那一档（tier 见 tools/extract_tables.py）。
      tier 0 = 正常能用的（可玩角色 / 给角色的物品）
      tier 1 = 次一级（基础形态 / 常规物品）
      tier 2 = 其它形态（皮肤变体、娃娃、头像、形象、装饰…）

    若最优档仍有多条，视为真歧义，返回全部候选，不瞎猜。
    allow_unavailables=True 时取消分级，所有同名条目平等参与（都变成候选）。
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
    if len(hits) > 1 and not allow_unavailables:
        best = min(_tier(h) for h in hits)
        hits = [h for h in hits if _tier(h) == best]
    if len(hits) == 1:
        return hits[0], []
    if len(hits) > 1:
        return None, list(hits)
    return None, []


def _tier(entry):
    t = entry.get("tier")
    return int(t) if t is not None else 2


def tier_name(entry):
    """给搜索结果标注：可玩 / 变体 / 娃娃 / 角色物品 / 常规物品 / 附属物。"""
    t = _tier(entry)
    if "grade" in entry or "name_en" in entry:      # 角色
        return ("可玩", "基础", "变体")[min(t, 2)]
    return ("角色物品", "常规物品", "附属物")[min(t, 2)]


def resolve_item(token, allow_unavailables=False):
    """解析物品。返回 (entry, candidates)。"""
    return _resolve(token, item_by_id, _item_by_name, allow_unavailables)


def resolve_char(token, allow_unavailables=False):
    """解析角色。返回 (entry, candidates)。

    默认优先可玩角色：例如「长命锁」命中 1001，而不是 100101 皮肤变体；
    「瞳」因为 9001/9002 同为可玩角色，会如实返回两个候选。
    """
    return _resolve(token, _char_by_id, _char_by_name, allow_unavailables)


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
    def by_tier(rows):
        return sorted(rows, key=lambda r: (_tier(r), int(r.get("id") or 0)))

    return by_tier(exact) + by_tier(prefix) + by_tier(contain)


def search_items(keyword):
    _ensure_loaded()
    return _search(_items, keyword)


def search_chars(keyword):
    _ensure_loaded()
    return _search(_chars, keyword)


def _by_id_or_name(token, by_id, by_name, allow_unavailables=False):
    """给附加表用的通用解析：纯数字按 ID，其它按名字。"""
    _ensure_loaded()
    token = str(token or "").strip()
    if not token:
        return None, []
    force_id = token.startswith("#")
    if force_id:
        token = token[1:].strip()
    if force_id or token.isdigit():
        if not token.isdigit():
            return None, []
        entry = by_id.get(int(token))
        return (entry, []) if entry else (None, [])
    hits = list(by_name.get(token) or [])
    if len(hits) > 1 and not allow_unavailables:
        best = min(_tier(h) for h in hits)
        hits = [h for h in hits if _tier(h) == best]
    if len(hits) == 1:
        return hits[0], []
    if len(hits) > 1:
        return None, hits
    return None, []


def resolve_currency(token):
    """解析货币。ID 是字符串（crystal / gold …），也支持按货币 ID 查。"""
    _ensure_loaded()
    token = str(token or "").strip()
    if not token:
        return None, []
    entry = _currency_by_id.get(token.lower())
    if entry:
        return entry, []
    # 也允许传物品 ID
    if token.isdigit():
        for c in currencies:
            if c.get("item_id") == int(token):
                return c, []
    return None, []


def resolve_activity(token, allow_unavailables=False):
    """解析活动（按 activityId 或名字）。"""
    return _by_id_or_name(token, _activity_by_id, _activity_by_name, allow_unavailables)


def resolve_task(token, _allow_unavailables=False):
    """解析任务（按 uiD）。"""
    _ensure_loaded()
    token = str(token or "").strip()
    if token.startswith("#"):
        token = token[1:].strip()
    if token.isdigit():
        entry = _task_by_id.get(int(token))
        return (entry, []) if entry else (None, [])
    hits = [t for t in _tasks if token and token in (t.get("desc") or "")]
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def resolve_map(token, allow_unavailables=False):
    """解析地图（按 referenceId 或房间名）。"""
    return _by_id_or_name(token, _map_by_id, _map_by_name, allow_unavailables)


def resolve_gacha(token, allow_unavailables=False):
    """解析卡池。"""
    return _by_id_or_name(token, _gacha_by_id, _gacha_by_name, allow_unavailables)


def resolve_story(token):
    """解析剧情（按 dialogueName）。"""
    _ensure_loaded()
    token = str(token or "").strip()
    if not token:
        return None, []
    entry = _story_by_id.get(token)
    if entry:
        return entry, []
    low = token.lower()
    hits = [s for s in _stories if low in str(s["id"]).lower()]
    if len(hits) == 1:
        return hits[0], []
    return None, hits[:20]


def list_playable_chars():
    """所有可玩角色（tier 0），按 ID 排序。"""
    _ensure_loaded()
    return [c for c in _chars if _tier(c) == 0]


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
    _log.info(stats())
    for tok in ("1001", "#1001", "长命锁", "龙玉", "不存在的名字"):
        e, cands = resolve_item(tok)
        if e:
            _log.info('{:<10} -> {}'.format(tok, e["name"]))
        elif cands:
            _log.info('{:<10} -> 重名 {:d} 条: {}'.format(tok, len(cands), [c["id"] for c in cands]))
        else:
            _log.info(f'{tok:<10} -> 未找到')
    _log.info("")
    for r in search_items("长命")[:6]:
        _log.info("  search 长命:", (r["id"], r["name"],))
    _log.info("")
    for tok in ("crystal", "101"):
        e, c = resolve_currency(tok)
        _log.info(f'  currency {tok:<8} -> {e}')
    for tok in ("无常", "1020", "寒衣卡池"):
        e, c = resolve_char(tok) if not tok.endswith("卡池") else resolve_activity(tok)
        _log.info('  {:<10} -> {}'.format(tok, e and (e.get("name"), e.get("id"))))
