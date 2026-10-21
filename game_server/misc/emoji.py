#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情播放（P0-E2 第 1 步）。

RpcPlayActEmoji 是 AOI 广播：单机没有别的玩家，服务端记一下最后用的表情
就够了，不回包（它是单向消息）。
"""
from ..rpc_common import decode_params, state

def _play_act_emoji(params, uid=None):
    """RpcPlayActEmoji [emoji_id] —— AOI 表情广播，单机无别人，记录即可。"""
    vals = decode_params(params)
    if isinstance(vals, list) and vals:
        try:
            state(uid).props["_oo_last_emoji"] = int(vals[0] or 0)
        except Exception:
            pass
    return []
