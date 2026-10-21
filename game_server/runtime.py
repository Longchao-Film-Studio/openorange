#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.runtime —— 运行时可变状态（进程内）。

和 config.py 的区别：
  config.py  启动时确定的配置（命令行/环境变量）
  runtime.py 运行中可以被管理接口改的状态

注意：副本关卡覆盖**不在这里**。
它是**按账号**的，存在玩家存档的 _oo_dungeon_force_level 私有字段里
（_oo_ 前缀的字段在 ms._live_init_dict 里会被过滤掉，客户端看不到）。
放存档里而不是进程内存里，好处：
  - 按账号隔离，多开互不影响
  - 重启游戏服不丢
"""

# 管理接口启动时间（/health 用）
STARTED_AT = None

# 启动时确定、运行中不变的副本默认行为（命令行 --dungeon-last-only）
CLI_DUNGEON_LAST_ONLY = False


def set_cli_dungeon_last_only(on):
    global CLI_DUNGEON_LAST_ONLY
    CLI_DUNGEON_LAST_ONLY = bool(on)
    return CLI_DUNGEON_LAST_ONLY


def snapshot():
    """给管理接口 /state 用的快照。"""
    return {
        "cli_dungeon_last_only": CLI_DUNGEON_LAST_ONLY,
        "started_at": STARTED_AT,
    }
