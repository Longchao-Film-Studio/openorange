#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨业务域共用的 RPC 基础设施（P0-E2）。

拆 entity_rpc.py 时发现，有四个东西几乎每个业务域都要用：

    state(uid)          服务端可变玩家状态（懒加载）
    sync_frame(...)     统一属性同步帧构造器
    throttled_save(...) 节流落盘
    decode_params(...)  msgpack 参数解码

如果把它们留在 entity_rpc.py，拆出去的模块就得反过来 import 装配层，
立刻变成循环依赖。所以单独抽一层，依赖方向固定为：

    业务域模块  ->  rpc_common  ->  ms / property / player_state
    装配层(dispatch) -> 各业务域模块

"""
import time

from . import ms as msmod
from .property import sync as _sync

# 事件链递归深度上限（防御外部数据里的环）
EVENT_MAX_DEPTH = 8

# 位置上报很频繁（每次移动都发），不能每次都写盘。
# 同一个 uid 每 5 秒最多落盘一次；正常退出/切场景时 force 写。
_LAST_SAVE = {}
_SAVE_INTERVAL = 5.0


def decode_params(params):
    """把 msgpack 参数解成 Python 值，解不出来就返回 None。"""
    if not params:
        return None
    try:
        val, _ = msmod.mp_decode(params)
        return val
    except Exception:
        return None


def state(uid=None):
    """服务端可变玩家状态（按 uid 分开，懒加载，首次用 initDict 初始化）。"""
    from . import player_state as pstate
    # 和 ms._live_init_dict 保持一致：状态里一律用整数键
    props = msmod._conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    return pstate.get_state(props, uid=uid or pstate.DEFAULT_UID)


def sync_frame(ownerid, property_id, op_type, name, value, session_id=""):
    """统一的属性同步帧构造器 —— 所有产生同步帧的地方都应该用它。

    ★ 为什么要有这层（2026-10-21 审计 P0-1/P0-2/P0-3）：
      原先 entity_rpc.py 里有 30 处直接调 msmod.build_property_sync，
      它们绕过了 sync 层的剥离和校验，于是：
        - 写嵌套容器时把 __tp__ 发出去 -> 客户端 rochange/roinit 崩
        - name 是 int -> ms.pb_bytes_field 里 len(int) 抛 TypeError
        - 非法帧照发，没人拦

    这层做三件事：
      1. 剥私有键（_oo_* 永远剥；__tp__ 只在 propertyId=0 的根属性值里保留）
      2. name 统一转字符串
      3. 不做校验（校验在 sync.validate，测试里跑 strict 模式）

    参数顺序和 msmod.build_property_sync 完全一致，方便整段替换。
    """
    if op_type in (msmod.OP_DEL, msmod.OP_REMOVE):
        val = None
    else:
        # 只有根属性值（propertyId == 0）保留 __tp__
        val = _sync._strip_private(value, is_root=(int(property_id) == 0))
    if not isinstance(name, str):
        name = str(name)
    return msmod.build_property_sync(ownerid, property_id, op_type, name,
                                     val, session_id)


def throttled_save(st, uid, force=False):
    """节流落盘：同一账号 5 秒内最多写一次。"""
    key = uid or ""
    now = time.time()
    if not force and (now - _LAST_SAVE.get(key, 0)) < _SAVE_INTERVAL:
        return
    _LAST_SAVE[key] = now
    try:
        st.save()
    except Exception as e:
        print("[save] 落盘失败 %s: %s" % (key, e), flush=True)
