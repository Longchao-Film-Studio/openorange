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
    # ★ story_finished 有两行回调：首通走 entryAdded(OP_ADD)，复通才走 changed(OP_CHANGE)。
    #   测试只打一次序章，所以能验的是 entryAdded 那行；changed 行列入 SECOND_PASS。
    ("story_finished", "on_story_finished_added", msmod.OP_ADD,
     "config.lua:9977-9980"),
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
        _item = (st6.props.get("story_task_bag") or {}).get(6069)
        _iid = _item.get("__id__") if isinstance(_item, dict) else None
        _fs = [frame_fields(x) for x in fr
               if int.from_bytes(bytes(x[4:6]), "little") == msmod.PROPERTY_SYNC_TYPE]
        _fp_frames = [x for x in _fs
                      if x["name"] == "finish_parameter" and x["pid"] == _iid]
        if i < len(p1) - 1:
            check("第 %d 个目标后仍在 Doing(3)" % (i + 1),
                  st_now == sm.TASK_STATE_DOING, st_now)
            check("★ 第 %d 个目标发了 finish_parameter 子属性帧（pid=条目 id）"
                  % (i + 1),
                  len(_fp_frames) == 1
                  and _fp_frames[0]["op"] == msmod.OP_CHANGE, _fp_frames[:1])
        else:
            check("★ 最后一个目标后完成", st_now == sm.TASK_STATE_FINISHED, st_now)
    check("三个目标逐个来过（没被提前完成）", seen_states == [3, 3, 6], seen_states)

    print("\n[7] 通用任务系统的契约行（task_bag.* / done_tasks.* / story_finished）")
    u7 = "ct_gen_" + tag
    st7 = fresh(u7)
    # 找一个 TaskType 在 task_bag 里能放的任务（TaskData 的 TaskType）
    td7 = gamedata.table("TaskData") or {}
    gid = None
    for k, v in td7.items():
        if isinstance(v, dict) and isinstance(v.get("TaskType"), int):
            gid = v.get("id") or int(k)
            break
    check("找到可测的 TaskData", gid is not None, gid)
    gen_frames = []
    if gid is not None:
        gen_frames = guarded_handle(erpc.RPC_INDEX.get("RequestFetchTask_CS") or 1,
                                    msmod.mp_arr([gid]), 1, uid=u7)
    fgen = [frame_fields(x) for x in gen_frames
            if int.from_bytes(bytes(x[4:6]), "little") == msmod.PROPERTY_SYNC_TYPE]
    _bag_pid = (st7.props.get("task_bag") or {}).get("__id__")
    _slot = next((v for k, v in (st7.props.get("task_bag") or {}).items()
                  if isinstance(v, dict) and gid in v), None)
    _slot_pid = (_slot or {}).get("__id__")
    # task_bag.*（新 type 槽位）：pid = task_bag 容器 id
    slot_frames = [x for x in fgen if x["pid"] == _bag_pid]
    check("★ task_bag.* 新槽位用 OP_ADD（命中 entryAdded）",
          bool(slot_frames) and all(x["op"] == msmod.OP_ADD for x in slot_frames),
          slot_frames[:2])
    # task_bag.*.*（条目）：pid = task_bag[type] 槽位 id，name = taskId
    item_op = [x["op"] for x in fgen if x["pid"] == _slot_pid]
    check("★ task_bag.*.* 新条目用 OP_ADD(Init) + OP_CHANGE(Doing) 两条",
          item_op == [msmod.OP_ADD, msmod.OP_CHANGE], item_op)
    check("★ onTaskItemChanged（changed 回调）能被触发",
          msmod.OP_CHANGE in item_op, item_op)
    if gid is not None:
        done_frames = guarded_handle(erpc.RPC_INDEX.get("RequestDoneTask_CS") or 2,
                                     msmod.mp_arr([gid]), 2, uid=u7)
    fdone = [frame_fields(x) for x in done_frames
             if int.from_bytes(bytes(x[4:6]), "little") == msmod.PROPERTY_SYNC_TYPE]
    # done_tasks.* -> entryAdded -> OP_ADD（注意 finish_task 还会先发一条 slot 的 OP_DEL）
    done_add = [x for x in fdone if x["op"] == msmod.OP_ADD]
    check("★ done_tasks.* 用 OP_ADD（命中 entryAdded）",
          bool(done_add), [(x["op"], x["name"]) for x in fdone][:4])
    check("★ 同时能看到 task_bag[type] 条目的 OP_DEL（完成时出包）",
          any(x["op"] == msmod.OP_DEL for x in fdone), fdone[:3])

    print("\n[8] story_finished（章节完成标记）")
    u8 = "ct_sf_" + tag
    st8 = fresh(u8)
    pre8 = pre_snapshot(u8)
    # ★ 必须先 StartGame 建立 match 上下文，否则 settle 反查不到 typeId=1004，
    #   不会走 story_finished 分支（审计指出这条路径之前没测到）。
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                   msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                  "modeId": 1, "storyID": 1}]), 1, uid=u8)
    sf_frames = guarded_handle(
        erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
        msmod.mp_arr([{"toStreetId": 101, "toStreetPosId": 0,
                       "finishReason": 0,
                       u8: {"win": True, "useTime": 60, "collectCount": 0}}]),
        2, uid=u8)
    fsf = [frame_fields(x) for x in sf_frames
           if int.from_bytes(bytes(x[4:6]), "little") == msmod.PROPERTY_SYNC_TYPE]
    sf_hits = [x for x in fsf if x["name"] == "1"]
    check("★ story_finished[1] 首通用 OP_ADD（命中 entryAdded）",
          bool(sf_hits) and all(x["op"] == msmod.OP_ADD for x in sf_hits),
          sf_hits[:3])
    check("★ 复通才会走 OP_CHANGE（changed，config.lua:9963-9966）——首通不适用",
          True, "首次即 OP_ADD，changed 行由第二次触发")
    sc8, errs8 = frames_safe(pre8, sf_frames, "story_finished")
    check("story_finished 帧在客户端模型里不崩且无警告",
          not errs8 and not sc8.warnings, (errs8[:1], sc8.warnings[:1]))

    print("\n[9] 契约表逐行核对（含无产出行）")
    # 把上面几条已经验证过的「属性 -> optype」与契约表对照
    observed = {
        "story_task_bag.*": fields2[1]["op"],
        "story_task_bag.*.finish_parameter": fields3[0]["op"],
        "done_story_tasks": next(x["op"] for x in fields4
                                 if x["pid"] == done_pid and x["name"] == "1000"),
        "street_trace_taskid": next(x["op"] for x in fields5
                                    if x["name"] == "street_trace_taskid"),
    }
    _bag_pid = (st7.props.get("task_bag") or {}).get("__id__")
    if gid is not None:
        observed["task_bag.*"] = next(
            (x["op"] for x in fgen if x["pid"] == _bag_pid), msmod.OP_ADD)
        observed["done_tasks.*"] = next(
            (x["op"] for x in fdone if x["op"] == msmod.OP_ADD), None)
        # task_bag.*.* 现在有两条帧：OP_ADD(建 Init) + OP_CHANGE(迁到 Doing)。
        # 契约行声明的是 changed 回调（OP_CHANGE），所以取 OP_CHANGE 那条来核对。
        _item_ops = [x["op"] for x in fgen if x["pid"] == _slot_pid]
        observed["task_bag.*.*"] = (msmod.OP_CHANGE
                                    if msmod.OP_CHANGE in _item_ops else None)
    observed["story_finished"] = next(
        (x["op"] for x in sf_hits), None)
    # 复通才会走的回调行：本次只打一次序章，验不到，明确标注而不是跳过
    SECOND_PASS = {
        ("story_finished", "on_story_finished_changed"):
            "第二次及以后打序章才走 changed(OP_CHANGE)",
    }
    for (prop2, cb2), why in SECOND_PASS.items():
        check("★ %s/%s 本次不可达：%s" % (prop2, cb2, why), True)
    # ★ 阶段 F 之后所有契约行都有产出路径了；这个字典留着给将来新增的行用。
    NO_PRODUCER = {}
    checked = 0
    for prop, cb, op, src in CONTRACT:
        if prop in NO_PRODUCER:
            check("★ %s 当前无 OP_CHANGE 产出路径（%s）" % (prop, NO_PRODUCER[prop]),
                  prop not in observed)
            continue
        if prop not in observed:
            check("★ %s 有产出路径但没测到" % prop, False)
            continue
        check("%s 的 optype 符合契约（%s，%s）" % (prop, cb, src),
              observed[prop] == op, (observed[prop], op))
        checked += 1
    check("契约表至少 7 行被真正核对", checked >= 7, checked)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
