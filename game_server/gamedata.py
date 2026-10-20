#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.gamedata —— 只读访问客户端的 BDD 数据表。

为什么需要它
------------
服务端要判断「某个道具用了会怎样」「体力上限是多少」，
这些规则全在客户端的 BDD 表里（ItemData / CommonConstData / LevelExpData …）。

路径
----
BDD 表不在仓库里（65 MB，来自游戏安装目录）。默认按
<repo>/../archive/game_data/BDD 找，可用环境变量 OO_BDD_DIR 或 config.BDD_DIR 覆盖。

**找不到时不会崩**：所有查询返回 None / 默认值，并只警告一次。
这样服务端在没装数据的环境里也能跑（只是没有道具效果）。

用法
----
    from . import gamedata
    gamedata.item(110)            # -> {'useType': 'avatarAttr', 'useParam': {...}, ...}
    gamedata.const('KeyNumMax')   # -> 180
    gamedata.level_exp(10)        # -> {'maxStaminaAdd': ..., ...}
"""
import json
import os
import threading

from . import config

_LOCK = threading.RLock()
_CACHE = {}
_WARNED = set()


def _path(name):
    return os.path.join(config.BDD_DIR, "TableData.CommonData.%s.json" % name)


def table(name):
    """按表名取整张表（dict 或 list）。读不到返回 None。"""
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
        p = _path(name)
        data = None
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                if name not in _WARNED:
                    _WARNED.add(name)
                    print("[data] %s 读取失败: %s" % (name, e), flush=True)
        else:
            if "dir" not in _WARNED:
                _WARNED.add("dir")
                print("[data] 警告：找不到 BDD 目录 %s —— 道具效果/体力恢复等规则不可用。"
                      % config.BDD_DIR, flush=True)
        _CACHE[name] = data
        return data


def _plain(entry, key, default=None):
    """BDD 的 OrderedDict 里混着 __bdd_metatype__ / KeyList，取纯值时过滤掉。"""
    if not isinstance(entry, dict):
        return default
    v = entry.get(key)
    if isinstance(v, dict):
        return v
    return default if v is None else v


def item(item_id):
    """ItemData[item_id]。"""
    d = table("ItemData")
    if not d:
        return None
    v = d.get(str(item_id)) or d.get(item_id)
    return v if isinstance(v, dict) else None


def item_use_param(item_id):
    """道具的使用参数：{TowerRewardKeys: 60, ...}（去掉 BDD 元数据键）。"""
    it = item(item_id)
    if not it:
        return {}
    up = it.get("useParam") or {}
    if not isinstance(up, dict):
        return {}
    return {k: v for k, v in up.items()
            if k not in ("KeyList", "__bdd_metatype__")}


def item_use_type(item_id):
    it = item(item_id)
    return (it or {}).get("useType")


def const(key, default=None):
    """CommonConstData[key]。"""
    d = table("CommonConstData")
    if not d:
        return default
    v = d.get(key)
    return default if v is None else v


def level_exp(level):
    """LevelExpData[level]（玩家等级配置，含 maxStaminaAdd）。

    注意：这张表在 BDD 里是 **list**（不是 dict），每条带 id 字段。
    """
    d = table("LevelExpData")
    if not d:
        return None
    if isinstance(d, dict):
        v = d.get(str(level)) or d.get(level)
        return v if isinstance(v, dict) else None
    for v in d:
        if isinstance(v, dict) and v.get("id") == level:
            return v
    return None


def story_task(task_id):
    d = table("StoryTaskData")
    if not d:
        return None
    v = d.get(str(task_id)) or d.get(task_id)
    return v if isinstance(v, dict) else None


def task(task_id):
    d = table("TaskData")
    if not d:
        return None
    v = d.get(str(task_id)) or d.get(task_id)
    return v if isinstance(v, dict) else None


def row(table_name, row_id):
    """按表名 + 行 id 取一行（字符串/整数键都试）。读不到返回 None。"""
    d = table(table_name)
    if not isinstance(d, dict):
        return None
    v = d.get(str(row_id))
    if v is None:
        v = d.get(row_id)
    return v


def task_data(task_id):
    """TaskData[task_id] —— 通用任务表，带 TriggerEventList（完成条件）。"""
    return row("TaskData", task_id)


def task_event(event_id):
    """TaskEventData[event_id] —— 触发条件，形如 {EventType:"FinishDungeon", EventParam:{...}}。"""
    return row("TaskEventData", event_id)


def street_prestige(street_id):
    """StreetPrestigeData[street_id] —— 该街区的声望等级表（list，含 needExpNum/level/levelAward）。"""
    d = table("StreetPrestigeData")
    if not d:
        return []
    v = d.get(str(street_id)) or d.get(street_id)
    return v if isinstance(v, list) else []


def all_system_catalogue():
    d = table("AllSystemCatalogueData")
    if not d:
        return []
    return list(d.values()) if isinstance(d, dict) else list(d)


def clear_cache():
    with _LOCK:
        _CACHE.clear()