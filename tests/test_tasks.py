#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 2：通用任务（TaskData）测试。

逆向依据 entities/avatarcomponent/avatartaskcomp.lua:256-284：
  getTaskState(taskType, taskID):
    task_bag[taskType][taskID] / done_tasks[taskType][taskID]

task_bag 是两层：task_bag[taskType] 是 TaskBag（Dict<taskId, TaskItem>），
依据 generated/config.lua:10123 的回调路径 ["task_bag.*.*"]。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_tasks_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, entity_rpc as erpc, player_state as pstate
from game_server import config, tasks

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] task_bag 类型必须是 TaskTypeBag（两层）")
    import json
    init = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "game_server", "avatar_init.json"),
        encoding="utf-8"))["__Properties__"]
    check("task_bag __tp__ = TaskTypeBag",
          (init.get("task_bag") or {}).get("__tp__") == "TaskTypeBag",
          (init.get("task_bag") or {}).get("__tp__"))

    print("\n[2] TaskData[1] 的 TaskType")
    t1 = tasks.task_type_of(1)
    check("TaskData[1] 有 TaskType", t1 is not None, t1)

    print("\n[3] 接任务：task_bag[type][1]")
    # ★ 不能用 --unlock-all：脚手架会把 Task 1 / 15 标成已完成，
    #   那正是要淘汰的假数据，会把这里要测的真流程挡住。
    config.configure_scaffold()
    msmod._live_init_dict("task_test")
    st = pstate.get_state(uid="task_test")
    frames = erpc.handle(erpc.RPC_INDEX["RequestFetchTask_CS"],
                         msmod.mp_arr([1]), 1, uid="task_test")
    check("返回同步帧", len(frames) >= 2, len(frames))
    tb = st.props.get("task_bag") or {}
    check("task_bag 有类型槽位", t1 in tb, list(tb.keys())[:5])
    slot = tb.get(t1) or {}
    check("槽位是 TaskBag", slot.get("__tp__") == "TaskBag", slot.get("__tp__"))
    check("槽位里有任务 1", 1 in slot, list(slot.keys())[:5])
    it = slot.get(1) or {}
    check("TaskItem __tp__", it.get("__tp__") == "TaskItem", it.get("__tp__"))
    check("state = Doing(3)", it.get("state") == 3, it.get("state"))
    for f in ("doing_targets", "finish_targets", "cache"):
        check("嵌套字段 %s 带 __id__" % f,
              isinstance((it.get(f) or {}).get("__id__"), int), it.get(f))
    check("task_state(1) = 3", tasks.task_state(st.props, 1) == 3)

    print("\n[4] 完成任务 1")
    frames = erpc.handle(erpc.RPC_INDEX["RequestDoneTask_CS"],
                         msmod.mp_arr([1]), 2, uid="task_test")
    check("返回同步帧", len(frames) >= 2, len(frames))
    check("task_bag[type] 里没有 1 了", 1 not in (st.props["task_bag"].get(t1) or {}))
    check("done_tasks[type][1] 有值",
          (st.props["done_tasks"].get(t1) or {}).get(1) is not None,
          st.props["done_tasks"].get(t1))
    check("task_state(1) = Finished(6)", tasks.task_state(st.props, 1) == 6)

    print("\n[5] 幂等：再完成一次不出错")
    erpc.handle(erpc.RPC_INDEX["RequestDoneTask_CS"],
                msmod.mp_arr([1]), 3, uid="task_test")
    check("状态仍是 6", tasks.task_state(st.props, 1) == 6)

    print("\n[6] RequestDoneSomeTypeTask_CS 批量完成")
    # 再接两个同类型任务
    same = []
    for tid in range(2, 40):
        if tasks.task_type_of(tid) == t1:
            same.append(tid)
            if len(same) >= 2:
                break
    check("找到同类型的其它任务", len(same) > 0, same)
    for tid in same:
        tasks.start_task(st, tid)
    erpc.handle(erpc.RPC_INDEX["RequestDoneSomeTypeTask_CS"],
                msmod.mp_arr([t1]), 4, uid="task_test")
    for tid in same:
        check("任务 %d 已完成" % tid, tasks.task_state(st.props, tid) == 6,
              tasks.task_state(st.props, tid))

    print("\n[7] 未知任务不崩")
    frames = erpc.handle(erpc.RPC_INDEX["RequestFetchTask_CS"],
                         msmod.mp_arr([999999]), 5, uid="task_test")
    check("返回应答帧", len(frames) >= 1, len(frames))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())