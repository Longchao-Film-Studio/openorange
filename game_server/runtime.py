#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.runtime —— 运行时可变状态（进程内，管理接口改）。

和 config.py 的区别：
  config.py  启动时确定的配置（命令行/环境变量）
  runtime.py 运行中可以被管理接口改的状态

目前只有副本关卡覆盖：
  0          = 不覆盖，按 Level_CopyData.rootLevel 正常下发
  <levelId>  = 进副本时只下发这一关（该关卡必须属于这个副本的 rootLevel）
"""
import threading

_LOCK = threading.RLock()

# 0 = 不覆盖
_DUNGEON_FORCE_LEVEL = 0

# 管理接口启动时间（/health 用）
STARTED_AT = None


def set_dungeon_force_level(level):
    """设置副本强制关卡。level=0 表示取消。返回实际生效的值。"""
    global _DUNGEON_FORCE_LEVEL
    try:
        v = int(level or 0)
    except (TypeError, ValueError):
        v = 0
    with _LOCK:
        _DUNGEON_FORCE_LEVEL = v if v > 0 else 0
        return _DUNGEON_FORCE_LEVEL


def dungeon_force_level():
    with _LOCK:
        return _DUNGEON_FORCE_LEVEL


def snapshot():
    """给管理接口 /state 用的快照。"""
    with _LOCK:
        return {
            "dungeon_force_level": _DUNGEON_FORCE_LEVEL,
            "started_at": STARTED_AT,
        }
