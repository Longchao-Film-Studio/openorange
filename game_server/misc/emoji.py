#!/usr/bin/env python3
"""表情播放（P0-E2 第 1 步 → 审计 T3 补广播）。

协议（avataraoicomp.lua）：
  发送：PlayActEmoji(type, act_emoji_id) -> CallServer("RpcPlayActEmoji", ...) 单向
  接收：Avatar_Play_ActEmoji_SC(uid, type, act_emoji_id)
        —— 街区场景里 OnPlayEmoji(uid, type, id) 播别人的表情

★ 审计 T3（协议符合性）：以前只记状态不广播 → 表情是"单机哑剧"。
  单机私服没有别的玩家，广播语义等价于【回显给自己】——
  客户端 SC 收到自己的 uid 也会走 OnPlayEmoji 播放（与 Soul 帧回显同一闭环思路）。
  未来多人时，这里改成向同 street 空间的所有连接转发即可（框架不变）。
"""
import contextlib

from ..rpc_common import decode_params, state
from ..space.space import _s2c_entity
from ..write_guard import write_guard

# Avatar_Play_ActEmoji_SC 的 RPC 索引（genRpcIndex，见 rpc_hashes.tsv）
RPC_AVATAR_PLAY_ACT_EMOJI_SC = 1376557419


def _play_act_emoji(params, uid=None):
    #属性写入口，在这里开守卫（表情动作）。
    with write_guard("_play_act_emoji"):
        """RpcPlayActEmoji [type, emoji_id] —— 记状态 + 广播（单机=回显自己）。"""
        vals = decode_params(params)
        emoji_type = 0
        emoji_id = 0
        if isinstance(vals, list) and vals:
            try:
                emoji_type = int(vals[0] or 0)
            except (TypeError, ValueError):
                emoji_type = 0
            if len(vals) > 1:
                try:
                    emoji_id = int(vals[1] or 0)
                except (TypeError, ValueError):
                    emoji_id = 0
        with contextlib.suppress(Exception):
            state(uid).props["_oo_last_emoji"] = emoji_id
        # ★ T3：下发 Avatar_Play_ActEmoji_SC(uid, type, emoji_id)
        #   单向消息（rid=0）也能带 S2C 帧（dispatch 会把 out 列表拼进应答流）
        if emoji_id:
            return [_s2c_entity(
                "Avatar_Play_ActEmoji_SC", RPC_AVATAR_PLAY_ACT_EMOJI_SC,
                [uid or "", emoji_type, emoji_id])]
        return []
