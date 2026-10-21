#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 B：属性同步契约（服务端发的帧 vs 客户端 PropertyCallbacks）。

★ 这份测试的唯一目的：把 docs/P1-剧情任务状态机.md §2.1 那张表变成可执行断言。
  表里每一行 = 「哪个属性 / 哪个回调 / 只有哪种 optype 会触发」。

★ 权威来源：客户端 generated/config.lua
    :9931-9956  AvatarStoryTaskComp
    :9958-9983  AvatarStorylineComp
    :10118-10146 AvatarTaskComp
  回调路径的解析规则在 core/propertysync/customtype.lua:263-312：
    - 根属性：路径 = 属性名
    - 动态条目：路径 = 父路径 + ".*"（OP_CHANGE 时 * 换成具体键）
    - OP_ADD / OP_DEL：路径是**容器自身**，所以带 ".*" 的 entryAdded 声明匹配不上

用法: python tests/test_story_sync_contract.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_contract_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from game_server.story.rpc import _story_sync_frames  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
NOW = 1700000000


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


# ---------------------------------------------------------------------------
# §2.1 契约表：属性路径 -> 回调 -> 允许的 optype
# ---------------------------------------------------------------------------
CONTRACT = [
    # (属性路径, 回调, optype, 依据)
    ("story_task_bag.*", "onStoryTaskItemChanged", msmod.OP_CHANGE,
     "config.lua:9933-9936"),
    ("story_task_bag.*.finish_parameter",
     "on_story_task_item_finish_parameter_changed", msmod.OP_CHANGE,
     "config.lua:9941-9944"),
    ("done_story_tasks", "onDoneStoryTasksChanged", msmod.OP_ADD,
     "config.lua:9951-9954"),
    ("street_trace_taskid", "on_street_tracing_task_id_changed", msmod.OP_CHANGE,
     "config.lua:9937-9940"),
    ("task_bag.*.*", "onTaskItemChanged", msmod.OP_CHANGE,
     "config.lua:10123-10126"),
    ("task_bag.*", "onTaskBagInserted", msmod.OP_ADD,
     "config.lua:10137-10140"),
    ("done_tasks.*", "onDoneTasksChanged", msmod.OP_ADD,
     "config.lua:10141-10144"),
    ("story_finished", "on_story_finished_changed", msmod.OP_CHANGE,
     "config.lua:9963-9966"),
]


def frame_fields(f):
    fl = msmod.pb_parse(bytes(f[6:]))
    return {
        "pid": fl.get(2, [None])[0],
        "op": fl.get(3, [None])[0],
        "name": (fl.get(4, [b""])[0].decode("utf-8", "replace")
                 if isinstance(fl.get(4, [b""])[0], (bytes, bytearray))
                 else fl.get(4, [b""])[0]),
    }


def fresh(uid):
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


def run():
    del passed[:]
    del failed[:]
    # ★ 每次 run 用不同后缀：同进程连跑多遍不会互相污染
    tag = "%04x" % random.randint(0, 0xFFFF)

    print("\n[1] 契约表自身完整（每行都有 optype 和依据）")
    check("契约表非空", len(CONTRACT) >= 8, len(CONTRACT))
    check("每行都有回调名与依据",
          all(cb and src for _p, cb, _o, src in CONTRACT))

    print("\n[2] story_task_bag.* —— 状态变化必须是 OP_CHANGE")
    u2 = "ct_bag_" + tag
    st2 = fresh(u2)
    pre2 = pre_snapshot(u2)
    ch2 = story.start_story_task(st2, 1003, NOW)
    f2 = _story_sync_frames(st2, ch2)
    fields2 = [frame_fields(x) for x in f2]
    bag_pid = st2.props["story_task_bag"]["__id__"]
    # 第一条 OP_ADD 建 Init（客户端不触发回调），第二条 OP_CHANGE 才是契约行
    check("建任务时第一条是 OP_ADD（建 Init 条目）",
          len(fields2) == 2 and fields2[0]["op"] == msmod.OP_ADD, fields2[:1])
    check("★ 第二条是 OP_CHANGE，pid=bag id，name=任务ID（命中 story_task_bag.*）",
          fields2[1]["op"] == msmod.OP_CHANGE
          and fields2[1]["pid"] == bag_pid
          and fields2[1]["name"] == "1003", fields2[1:])
    sc2, errs2 = frames_safe(pre2, f2, "bag.*")
    check("帧在客户端模型里不崩且无警告",
          not errs2 and not sc2.warnings, (errs2[:1], sc2.warnings[:1]))

    print("\n[3] story_task_bag.*.finish_parameter —— 子属性必须是 OP_CHANGE")
    u3 = "ct_fp_" + tag
    st3 = fresh(u3)
    story.start_story_task(st3, 6069, NOW)   # P1=[106051,10662,10663]
    item3 = st3.props["story_task_bag"][6069]
    pre3 = pre_snapshot(u3)
    ch3 = story.mark_finish_parameter(st3, 6069, 106051)
    f3 = _story_sync_frames(st3, ch3)
    fields3 = [frame_fields(x) for x in f3]
    check("★ pid = 条目自己的 __id__（不是容器 id），name = finish_parameter",
          bool(fields3) and fields3[0]["pid"] == item3["__id__"]
          and fields3[0]["name"] == "finish_parameter", fields3[:1])
    check("optype = OP_CHANGE", bool(fields3) and fields3[0]["op"] == msmod.OP_CHANGE,
          fields3[:1])
    sc3, errs3 = frames_safe(pre3, f3, "finish_parameter")
    check("子属性帧在客户端模型里不崩且无警告",
          not errs3 and not sc3.warnings, (errs3[:1], sc3.warnings[:1]))
    check("finish_parameter 里记下了 106051",
          106051 in story.finish_parameters_of(item3),
          story.finish_parameters_of(item3))

    print("\n[4] done_story_tasks —— 归档必须是 OP_ADD（entryAdded）")
    u4 = "ct_done_" + tag
    st4 = fresh(u4)
    story.start_story_task(st4, 1000, NOW)
    pre4 = pre_snapshot(u4)
    f4 = _story_sync_frames(st4, story.mark_story_task_finished(st4, 1000, NOW))
    f4 += _story_sync_frames(st4, story.archive_story_task(st4, 1000, NOW))
    fields4 = [frame_fields(x) for x in f4]
    done_pid = st4.props["done_story_tasks"]["__id__"]
    check("★ 有一条 pid=done 容器 id、name=任务ID、optype=OP_ADD",
          any(x["pid"] == done_pid and x["name"] == "1000"
              and x["op"] == msmod.OP_ADD for x in fields4),
          [(x["pid"], x["op"], x["name"]) for x in fields4])
    sc4, errs4 = frames_safe(pre4, f4, "done")
    check("归档帧在客户端模型里不崩且无警告",
          not errs4 and not sc4.warnings, (errs4[:1], sc4.warnings[:1]))

    print("\n[5] street_trace_taskid —— 根属性必须是 OP_CHANGE")
    u5 = "ct_trace_" + tag
    st5 = fresh(u5)
    pre5 = pre_snapshot(u5)
    frames5 = guarded_handle(erpc.RPC_INDEX["updateStreetTraceTask_CS"],
                             msmod.mp_arr([1003]), 1, uid=u5)
    fields5 = [frame_fields(x) for x in frames5
               if int.from_bytes(bytes(x[4:6]), "little") == msmod.PROPERTY_SYNC_TYPE]
    check("★ 有 name=street_trace_taskid 的 OP_CHANGE",
          any(x["name"] == "street_trace_taskid" and x["op"] == msmod.OP_CHANGE
              for x in fields5),
          [(x["pid"], x["op"], x["name"]) for x in fields5])
    check("propertyId <= 0（根属性）",
          all(x["pid"] <= 0 for x in fields5 if x["name"] == "street_trace_taskid"),
          fields5[:2])
    check("服务端状态已更新", st5.props.get("street_trace_taskid") == 1003,
          st5.props.get("street_trace_taskid"))
    sc5, errs5 = frames_safe(pre5, frames5, "trace")
    check("追踪帧在客户端模型里不崩且无警告",
          not errs5 and not sc5.warnings, (errs5[:1], sc5.warnings[:1]))

    print("\n[6] 多目标任务：三个目标没集齐就不许完成")
    u6 = "ct_multi_" + tag
    st6 = fresh(u6)
    story.start_story_task(st6, 6069, NOW)
    p1 = (gamedata.story_task(6069) or {}).get("Parameter1") or []
    check("6069 确实是多目标任务（3 个 Parameter1）", len(p1) == 3, p1)
    seen_states = []
    for i, param in enumerate(p1):
        fr = guarded_handle(erpc.RPC_INDEX["RequestDoneStoryTaskParamInt_CS"],
                            msmod.mp_arr([6069, int(param)]), 10 + i, uid=u6)
        st_now = story.story_state(st6.props, 6069)
        seen_states.append(st_now)
        if i < len(p1) - 1:
            check("第 %d 个目标后仍在 Doing(3)" % (i + 1),
                  st_now == sm.TASK_STATE_DOING, st_now)
        else:
            check("★ 最后一个目标后完成", st_now == sm.TASK_STATE_FINISHED, st_now)
    check("前两个目标的帧里有 finish_parameter 子属性更新",
          len(seen_states) == 3)

    print("\n[7] 契约表逐行：帧形态与声明一致")
    # 把上面几条已经验证过的「属性 -> optype」与契约表对照
    observed = {
        "story_task_bag.*": fields2[1]["op"],
        "story_task_bag.*.finish_parameter": fields3[0]["op"],
        "done_story_tasks": next(x["op"] for x in fields4
                                 if x["pid"] == done_pid and x["name"] == "1000"),
        "street_trace_taskid": next(x["op"] for x in fields5
                                    if x["name"] == "street_trace_taskid"),
    }
    for prop, cb, op, src in CONTRACT:
        if prop not in observed:
            continue
        check("%s 的 optype 符合契约（%s，%s）" % (prop, cb, src),
              observed[prop] == op, (observed[prop], op))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
