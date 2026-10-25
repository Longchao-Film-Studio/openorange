#!/usr/bin/env python3
"""NPC 手机短信的三个实体 RPC（avatarnpcchatcomp.lua:26-95）。

逆向依据：
  Check_TopicID_CanOpen_CS {topicId}                    :65-70（回调体为空，不消费返回值）
  Update_NpcChat_Info_CS {chat_type, chat_id, topic_id,
                          info_id, info_val, is_over}   :72-81
  GmRpcOpenNpcChatTopic {topicId}                       avatardebugcomp.lua:92（GM）

所有状态写入都走 story/npc_sms.py（唯一入口，R8）。
"""
from . import gamedata
from .story import npc_sms
from .rpc_common import respond_after_sync, decode_params, state

from shared.log_tee import get_logger
_log = get_logger(__name__)


def _check_topic_can_open(rid, params, uid=None):
    """Check_TopicID_CanOpen_CS {topicId} —— 回 0/1；客户端回调是空函数。"""
    syncs = []
    vals = decode_params(params)
    topic = int(vals[0]) if (isinstance(vals, list) and vals) else 0
    st = state(uid)
    opened = 0
    cfg = gamedata.npc_message(topic)
    if isinstance(cfg, dict):
        try:
            chat_id = int(cfg.get("role"))
            opened = 1 if npc_sms.is_unlocked(st, chat_id, topic) else 0
        except Exception:
            opened = 0
    return respond_after_sync(rid, syncs, ret={"status": True, "can_open": opened})


def _update_npc_chat_info(rid, params, uid=None):
    """Update_NpcChat_Info_CS {chat_type, chat_id, topic_id, info_id, info_val, is_over}。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and len(vals) >= 6):
        _log.warning("[sms] Update_NpcChat_Info_CS 参数不足: %r", vals)
        return respond_after_sync(rid, syncs)
    try:
        int(vals[0])              # chat_type：契约第 1 参，服务端不消费但必须能解析
        chat_id = int(vals[1])
        topic_id = int(vals[2])
        info_id = int(vals[3])
        info_val = int(vals[4])
        is_over = int(vals[5])
    except Exception as e:
        _log.warning("[sms] Update_NpcChat_Info_CS 参数非法: %r (%s)", vals, e)
        return respond_after_sync(rid, syncs)
    st = state(uid)
    syncs.extend(npc_sms.mark_topic_progress(
        st, chat_id, topic_id, info_id, info_val, is_over))
    return respond_after_sync(rid, syncs, ret={"status": True})


def _gm_open_npc_chat_topic(rid, params, uid=None):
    """GmRpcOpenNpcChatTopic {topicId} —— GM 强制解锁（显式越权，跳过映射检查）。"""
    syncs = []
    vals = decode_params(params)
    topic = int(vals[0]) if (isinstance(vals, list) and vals) else 0
    st = state(uid)
    cfg = gamedata.npc_message(topic) or {}
    try:
        chat_id = int(cfg.get("role"))
    except Exception:
        chat_id = 0
    syncs.extend(npc_sms.unlock_topic(st, chat_id, topic,
                                      source="gm_rpc", allow_unmapped=True))
    return respond_after_sync(rid, syncs, ret={"status": True})
