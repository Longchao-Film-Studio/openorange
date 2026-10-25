#!/usr/bin/env python
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

from game_server import config, tasks
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []

def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))

def slot_holder(props, ttype):
    """取 task_bag[ttype] 槽位。"""
    bag = props.get("task_bag") or {}
    slot = bag.get(ttype)
    return slot if isinstance(slot, dict) else None


def run():
    import random
    tag = f"{random.randint(0, 0xFFFF):04x}"
    U = "task_test_" + tag
    del passed[:]
    del failed[:]
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
    msmod.live_init_dict(U)
    st = pstate.get_state(uid=U)
    frames = guarded_handle(erpc.RPC_INDEX["RequestFetchTask_CS"],
                         msmod.mp_arr([1]), 1, uid=U)
    check("返回同步帧", len(frames) >= 2, len(frames))
    # ★ P1 阶段 F：接任务必须是 OP_ADD(Init) + OP_CHANGE(Doing) 两条。
    #   客户端 changed["task_bag.*.*"] -> onTaskItemChanged 只对 OP_CHANGE 触发，
    #   entryAdded["task_bag.*"] -> onTaskBagInserted 是空函数。
    #   只发 OP_ADD 的话「接任务」在客户端一点反应都没有。
    _ops = []
    for _f in frames:
        if not isinstance(_f, (bytes, bytearray)) or len(_f) < 6:
            continue
        if int.from_bytes(bytes(_f[4:6]), "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        _fl = msmod.pb_parse(bytes(_f[6:]))
        _ops.append((_fl.get(2, [None])[0], _fl.get(3, [None])[0]))
    _slot_pid = (slot_holder(st.props, t1) or {}).get("__id__")
    _entry_ops = [op for pid, op in _ops if pid == _slot_pid]
    check("★ 条目帧是 OP_ADD(2) + OP_CHANGE(1) 两条",
          _entry_ops == [msmod.OP_ADD, msmod.OP_CHANGE], _entry_ops)
    check("★ 有 OP_CHANGE（onTaskItemChanged 能触发）",
          msmod.OP_CHANGE in _entry_ops, _entry_ops)
    tb = st.props.get("task_bag") or {}
    check("task_bag 有类型槽位", t1 in tb, list(tb.keys())[:5])
    slot = tb.get(t1) or {}
    check("槽位是 TaskBag", slot.get("__tp__") == "TaskBag", slot.get("__tp__"))
    check("槽位里有任务 1", 1 in slot, list(slot.keys())[:5])
    it = slot.get(1) or {}
    check("TaskItem __tp__", it.get("__tp__") == "TaskItem", it.get("__tp__"))
    check("state = Doing(3)", it.get("state") == 3, it.get("state"))
    for f in ("doing_targets", "finish_targets", "cache"):
        check(f"嵌套字段 {f} 带 __id__",
              isinstance((it.get(f) or {}).get("__id__"), int), it.get(f))
    check("task_state(1) = 3", tasks.task_state(st.props, 1) == 3)

    print("\n[4] 完成任务 1")
    frames = guarded_handle(erpc.RPC_INDEX["RequestDoneTask_CS"],
                         msmod.mp_arr([1]), 2, uid=U)
    check("返回同步帧", len(frames) >= 2, len(frames))
    # ★ 阶段 F：完成也要先 OP_CHANGE 到 Finished(6)，再 OP_DEL 出包。
    _ops2 = []
    for _f in frames:
        if not isinstance(_f, (bytes, bytearray)) or len(_f) < 6:
            continue
        if int.from_bytes(bytes(_f[4:6]), "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        _fl = msmod.pb_parse(bytes(_f[6:]))
        _ops2.append((_fl.get(2, [None])[0], _fl.get(3, [None])[0],
                     _fl.get(4, [b""])[0]))
    _chg = [o for o in _ops2 if o[1] == msmod.OP_CHANGE]
    _del = [o for o in _ops2 if o[1] == msmod.OP_DEL]
    check("★ 完成时有 OP_CHANGE（→Finished）", bool(_chg), _ops2)
    check("★ 完成时有 OP_DEL（出包）", bool(_del), _ops2)
    check("task_bag[type] 里没有 1 了", 1 not in (st.props["task_bag"].get(t1) or {}))
    check("done_tasks[type][1] 有值",
          (st.props["done_tasks"].get(t1) or {}).get(1) is not None,
          st.props["done_tasks"].get(t1))
    check("task_state(1) = Finished(6)", tasks.task_state(st.props, 1) == 6)

    print("\n[5] 幂等：再完成一次不出错")
    guarded_handle(erpc.RPC_INDEX["RequestDoneTask_CS"],
                msmod.mp_arr([1]), 3, uid=U)
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
    guarded_handle(erpc.RPC_INDEX["RequestDoneSomeTypeTask_CS"],
                msmod.mp_arr([t1]), 4, uid=U)
    for tid in same:
        check(f'任务 {tid} 已完成', tasks.task_state(st.props, tid) == 6,
              tasks.task_state(st.props, tid))

    print("\n[7] 未知任务不崩")
    frames = guarded_handle(erpc.RPC_INDEX["RequestFetchTask_CS"],
                         msmod.mp_arr([999999]), 5, uid=U)
    check("返回应答帧", len(frames) >= 1, len(frames))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
