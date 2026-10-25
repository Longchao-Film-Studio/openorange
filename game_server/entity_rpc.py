#!/usr/bin/env python3
"""实体 RPC（serverRpc）应答层 —— 兼容层（P0-E2）。

历史名字，保留是为了不改动 server.py / tests / tools 里已有的
from . import entity_rpc as erpc 和 erpc.xxx 调用点。

真正的实现在：
  dispatch.py      装配层（索引 -> 处理函数 + handle）
  rpc_index.py     索引表
  rpc_common.py    状态 / 帧构造 / 节流存档 / 参数解码
  property/ story/ space/ dungeon/ street/ misc/   各业务域

新增 RPC 请改对应业务域模块，不要再往这个文件里加代码。
"""
from .dispatch import *  # noqa: F401,F403
from .dispatch import (  # noqa: F401  (re-export 私有符号)
    _EVENT_MAX_DEPTH,
    _HANDLERS,
    _LAST_SAVE,
    _ONE_WAY_HANDLERS,
    _SAVE_INTERVAL,
    _drain_pending_chain,
    _emit_get_item,
    _finish_story_tasks,
    _state,
    _sync_frame,
    _throttled_save,
    handle,
)
