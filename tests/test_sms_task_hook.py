#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P6 短信任务钩子测试：任务推进时自动解锁短信（docs/计划-剧情短信RPC.md P6）。

不变量：
  INV-SMS-9  完成 1043 -> 1044 进入 Doing 的同一批帧里含 1014 的解锁帧
             （客户端一次收到，不需要第二次交互/重登）
  INV-SMS-10 钩子失败不影响任务推进（1044 照常 Doing）
  INV-SMS-11 无映射任务的推进完全无感（状态与帧数与旧逻辑一致）

用法: python tests/test_sms_task_hook.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_smshook_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import npc_sms  # noqa: E402
from game_server.story.rpc import _finish_story_tasks  # noqa: E402

passed, failed = [], []
NOW = 1700000000
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


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("")
    print("[1] INV-SMS-9：完成 1043 的同一批应答帧里，1014 已解锁")
    u1, st1 = fresh("h1")
    # 把 1043 摆成进行中（生产里它是 1048 完成后推进的）
    story.start_story_task(st1, 1043, NOW)
    frames = _finish_story_tasks(st1, [1043], param=101056)
    check("1044 进入进行中", story.story_state(st1.props, 1044) == 3,
          story.story_state(st1.props, 1044))
    check("解锁帧在应答帧流里（len>0）", len(frames) > 0, len(frames))
    check("1014 已解锁（状态侧）", npc_sms.is_unlocked(st1, 100103, 1014))
    unread = st1.props["npc_chat_prop"]["resident_topic_ids"]
    vals = [int(v) for v in unread.values()] if isinstance(unread, dict) else [int(v) for v in unread]
    check("未读队列含 1014", 1014 in vals, vals)

    print("")
    print("[2] INV-SMS-11：无映射任务的推进不受影响")
    u2, st2 = fresh("h2")
    story.start_story_task(st2, 1042, NOW)
    before = str(st2.props.get("npc_chat_prop"))
    _finish_story_tasks(st2, [1042], param=None)
    check("1042 完成、1048 推进", story.story_state(st2.props, 1048) == 3,
          story.story_state(st2.props, 1048))
    check("无映射 -> 短信属性完全没动", str(st2.props.get("npc_chat_prop")) == before)

    print("")
    print("[3] 幂等：重复完成不重复解锁（归档只走一次，这里验重复钩子）")
    f3 = npc_sms.unlock_for_task(st1, 1044, source="repeat")
    check("再次调用 unlock_for_task 为空帧", f3 == [])

    print("")
    print("=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
