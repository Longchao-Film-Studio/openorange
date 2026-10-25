#!/usr/bin/env python
"""剧情短信系统不变量测试（docs/计划-剧情短信RPC.md §8）。

每条不变量都对应一个「能失败的断言」；实现改回坏的样子必须红。
契约依据：gamecommon/customtypes/npcchat/npcchatprop.lua、generated/config.lua:4925-4992。

用法: python tests/test_npc_sms.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_sms_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story import npc_sms  # noqa: E402

passed, failed = [], []
_TAG = ""


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(name):
    u = "%s_%s" % (name, _TAG)
    msmod._live_init_dict(u)
    return u, pstate.get_state(uid=u)


def frames_ops(frames):
    """粗提 (optype, name) 序列——帧是字节，这里用 Tx 的 ops 侧信不方便，
    所以直接在 unlock 前后比对 props 并用 Tx 重新演算；更直接的办法：
    unlock_topic 返回帧列表长度 + 我们对 props 的断言。这里只断言帧数与状态。"""
    return frames


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print()
    print("[0] 数据前置：1014 话题/对话/角色都在，映射表含 1044->1014")
    cfg = gamedata.npc_message(1014)
    check("1014 话题存在且 chatType=1", isinstance(cfg, dict) and int(cfg.get("chatType")) == 1,
          cfg)
    check("1014 对话行存在（3+ 句）",
          isinstance(gamedata.npc_chat_dialog(1014), dict) and
          len(gamedata.npc_chat_dialog(1014)) >= 3)
    check("100103 角色存在（杜老爷）",
          "杜" in str((gamedata.npc_actor(100103) or {}).get("actorName")),
          (gamedata.npc_actor(100103) or {}).get("actorName"))
    check("映射表 1044->1014", npc_sms.topics_for_task(1044) == [1014])

    print()
    print("[1] INV-SMS-5：任务路径不能解锁映射表外话题")
    u1, st1 = fresh("sms1")
    f1 = npc_sms.unlock_topic(st1, 99999, 50019, source="test")
    check("表外话题（50019，任务路径）不解锁", f1 == [] and
          not npc_sms.is_unlocked(st1, 99999, 50019))
    f2 = npc_sms.unlock_topic(st1, 100133, 50019, source="test", allow_unmapped=True)
    check("GM 路径显式越权可解锁", len(f2) > 0 and npc_sms.is_unlocked(st1, 100133, 50019))

    print()
    print("[2] INV-SMS-3：幂等 —— 重复解锁返回空帧")
    u3, st3 = fresh("sms3")
    a3 = npc_sms.unlock_topic(st3, 100103, 1014, source="test", allow_unmapped=True)
    b3 = npc_sms.unlock_topic(st3, 100103, 1014, source="test", allow_unmapped=True)
    check("首次解锁有帧、再次为空", len(a3) > 0 and b3 == [], (len(a3), len(b3)))

    print()
    print("[3] INV-SMS-4/8：条目形状 —— 四字段齐全 + update_time 非 0")
    entry = ((st3.props.get("npc_chat_prop") or {}).get("address_list") or {}).get(100103, {}).get(1014)
    check("条目存在", isinstance(entry, dict), entry)
    if isinstance(entry, dict):
        for k in ("chat_type", "npc_id", "group_npc_ids", "is_over", "update_time", "topic_infos"):
            check("  条目含 %s" % k, k in entry)
        check("  update_time 非 0", int(entry.get("update_time") or 0) > 0)
        check("  npc_id = 会话角色", int(entry.get("npc_id")) == 100103)
    check("未读队列已入列 1014",
          1014 in [int(v) for v in (st3.props.get("npc_chat_prop") or {}).get("resident_topic_ids", {}).values()]
          if isinstance((st3.props.get("npc_chat_prop") or {}).get("resident_topic_ids"), dict)
          else 1014 in [int(v) for v in (st3.props.get("npc_chat_prop") or {}).get("resident_topic_ids", [])])

    print()
    print("[4] INV-SMS-6/7：进度上报 —— 未解锁忽略；is_over=1 出未读队列")
    u4, st4 = fresh("sms4")
    before = str(st4.props.get("npc_chat_prop"))
    f4 = npc_sms.mark_topic_progress(st4, 100103, 1014, 10491, 1, 1)
    check("未解锁收到进度 -> 忽略（无帧、状态不变）", f4 == [] and
          str(st4.props.get("npc_chat_prop")) == before)
    npc_sms.unlock_topic(st4, 100103, 1014, source="test", allow_unmapped=True)
    f5 = npc_sms.mark_topic_progress(st4, 100103, 1014, 10491, 1, 1)
    check("已解锁收到进度 -> 有帧", len(f5) > 0)
    e5 = st4.props["npc_chat_prop"]["address_list"][100103][1014]
    check("topic_infos 落库", e5["topic_infos"].get(10491) == 1, e5.get("topic_infos"))
    check("is_over 落库", int(e5["is_over"]) == 1)
    unread = st4.props["npc_chat_prop"]["resident_topic_ids"]
    vals = [int(v) for v in unread.values()] if isinstance(unread, dict) else [int(v) for v in unread]
    check("is_over=1 后出未读队列", 1014 not in vals, vals)

    print()
    print("[5] RPC 三个索引已注册且 handler 已挂")
    from game_server import dispatch as dd
    for nm in ("Check_TopicID_CanOpen_CS", "Update_NpcChat_Info_CS", "GmRpcOpenNpcChatTopic"):
        idx = rpc_index.RPC_INDEX.get(nm)
        check("  %s 有索引" % nm, idx is not None)
        check("  %s 有 handler" % nm, idx in dd._HANDLERS)

    print()
    print("[6] unlock_for_task：1044 解锁 1014；未映射任务只告警不动状态")
    u6, st6 = fresh("sms6")
    f6 = npc_sms.unlock_for_task(st6, 1044, source="test")
    check("1044 -> 自动解锁 1014", len(f6) > 0 and npc_sms.is_unlocked(st6, 100103, 1014))
    before6 = str(st6.props.get("npc_chat_prop"))
    f7 = npc_sms.unlock_for_task(st6, 1045, source="test")
    check("1045 无映射 -> 不解锁、状态不变", f7 == [] and
          str(st6.props.get("npc_chat_prop")) == before6)

    print()
    print("=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
