#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 A：剧情任务状态机（唯一写入口）。

★ 不变量（本文件逐条验证）

  1. 状态只能沿 Init(2)→Doing(3)→WaitDone(4)→Done(5)→Finished(6) 单向前进，
     表外组合抛 TransitionError。
  2. 每次状态变化产生且只产生一条 ("sub", "story_task_bag", taskId, item)，
     由 _story_sync_frames 编成 OP_CHANGE。
  3. 只有 Finished 的任务能归档进 done_story_tasks。
  4. 全仓只有 state_machine.py 允许改 story_task_bag[*]["state"]（AST 检查）。

★ 客户端依据
  generated/config.lua:9931-9956   changed["story_task_bag.*"] -> onStoryTaskItemChanged
  avatarstorytaskcomp.lua:557-558  onStoryTaskBagInserted 是空函数（OP_ADD 无 UI）
  avatarstorytaskcomp.lua:409-434  old.state < Doing && new.state == Doing -> 接任务 UI
  avatarstorytaskcomp.lua:376-435  old.state ~= Finished && new.state == Finished -> 场记板

用法: python tests/test_story_state_machine.py
"""
import ast
import glob
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_sm_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from game_server.story.rpc import _story_sync_frames  # noqa: E402
from tests.frame_guard import pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1700000000
# ★ 每次 run() 用不同的 uid 后缀：同一个进程里跑两遍不会互相污染
_TAG = ""


def _unwrap(v):
    """影子缓存里的自定义类型是 CustomObj（字段在 .props 里）。"""
    return v.props if hasattr(v, "props") else v


def sc_state(sc, task_id, container="story_task_bag"):
    """从影子缓存里读某任务的状态。"""
    bag = _unwrap(sc.props.get(container))
    if not isinstance(bag, dict):
        return None
    item = _unwrap(bag.get(task_id))
    if not isinstance(item, dict):
        return None
    return item.get("state")


def uid_of(name):
    return "%s_%s" % (name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 迁移表完整性")
    check("8 个状态常量值正确",
          (sm.TASK_STATE_IDLE, sm.TASK_STATE_INIT, sm.TASK_STATE_DOING,
           sm.TASK_STATE_WAIT_DONE, sm.TASK_STATE_DONE, sm.TASK_STATE_FINISHED,
           sm.TASK_STATE_FAILED, sm.TASK_STATE_CLOSED) == (1, 2, 3, 4, 5, 6, -1, -2))
    check("所有迁移的 from/to 都是已定义状态",
          all(a in sm._STATE_NAME and b in sm._STATE_NAME for a, b in sm.TRANSITIONS))
    check("迁移表里没有自环", all(a != b for a, b in sm.TRANSITIONS))
    bad = [(a, b) for a, b in sm.TRANSITIONS if (b, a) in sm.TRANSITIONS]
    check("不存在双向迁移（状态只许单向前进）", not bad, bad)
    seen, q = {sm.TASK_STATE_INIT}, [sm.TASK_STATE_INIT]
    while q:
        cur = q.pop(0)
        for a, b in sm.TRANSITIONS:
            if a == cur and b not in seen:
                seen.add(b)
                q.append(b)
    check("从 Init 能走到 Finished", sm.TASK_STATE_FINISHED in seen, seen)
    check("Init 不能回到 Idle",
          not sm.can_transition(sm.TASK_STATE_INIT, sm.TASK_STATE_IDLE))
    check("Finished 是终点",
          not any(a == sm.TASK_STATE_FINISHED for a, _b in sm.TRANSITIONS))

    print("\n[2] create_task：OP_ADD 建 Init 条目")
    st = fresh("sm_a")
    # ★ 1000 会被 init_new_player 自动设为 Doing，所以用 1003 测 create
    ch = sm.create_task(st, 1003, NOW)
    check("返回一条 add 描述", len(ch) == 1 and ch[0][0] == "add", ch)
    check("容器是 story_task_bag", bool(ch) and ch[0][1] == "story_task_bag", ch)
    it = st.props["story_task_bag"][1003]
    check("新条目状态是 Init(2)", it.get("state") == sm.TASK_STATE_INIT, it.get("state"))
    check("__tp__ = StoryTaskItem", it.get("__tp__") == "StoryTaskItem")
    check("嵌套容器都带 __id__",
          all(isinstance((it.get(f) or {}).get("__id__"), int)
              for f in ("doing_targets", "finish_targets", "finish_parameter")))
    check("重复 create 返回空", sm.create_task(st, 1003, NOW) == [])

    print("\n[3] transition：产生且只产生一条 OP_CHANGE 描述")
    ch = sm.transition(st, 1003, sm.TASK_STATE_DOING, source="test", now=NOW)
    check("返回一条描述", len(ch) == 1, ch)
    check("描述是 sub/story_task_bag",
          bool(ch) and ch[0][0] == "sub" and ch[0][1] == "story_task_bag", ch)
    check("value 就是条目本身（含新 state）",
          bool(ch) and ch[0][3].get("state") == sm.TASK_STATE_DOING)
    check("同状态重复迁移幂等",
          sm.transition(st, 1003, sm.TASK_STATE_DOING, source="again") == [])

    print("\n[4] 非法迁移必须抛 TransitionError")
    for frm, to in ((sm.TASK_STATE_FINISHED, sm.TASK_STATE_DOING),
                    (sm.TASK_STATE_DOING, sm.TASK_STATE_INIT)):
        st2 = fresh("sm_bad_%d_%d" % (frm, to))
        sm.create_task(st2, 1003, NOW)
        if frm != sm.TASK_STATE_INIT and sm.can_transition(sm.TASK_STATE_INIT, frm):
            sm.transition(st2, 1003, frm, source="setup")
        raised = False
        try:
            sm.transition(st2, 1003, to, source="bad")
        except sm.TransitionError:
            raised = True
        check("非法迁移 %s->%s 抛异常" % (sm.state_name(frm), sm.state_name(to)), raised)

    print("\n[5] 不在包里的任务不能迁移")
    st3 = fresh("sm_c")
    raised = False
    try:
        sm.transition(st3, 9999, sm.TASK_STATE_DOING, source="test")
    except sm.TransitionError:
        raised = True
    check("迁移不存在的任务抛 TransitionError", raised)

    print("\n[6] move_to_done：只有 Finished 才能归档")
    st4 = fresh("sm_d")
    sm.create_task(st4, 1003, NOW)
    raised = False
    try:
        sm.move_to_done(st4, 1003)
    except sm.TransitionError:
        raised = True
    check("未 Finished 就归档 -> 抛异常", raised)
    sm.transition(st4, 1003, sm.TASK_STATE_DOING, source="t")
    sm.transition(st4, 1003, sm.TASK_STATE_FINISHED, source="t")
    ch = sm.move_to_done(st4, 1003)
    kinds = [c[0] for c in ch]
    check("归档返回 add + del", kinds == ["add", "del"], kinds)
    check("done 里有 1003", 1003 in st4.props["done_story_tasks"])
    check("bag 里没有 1003", 1003 not in st4.props["story_task_bag"])
    check("story_state 读到 Finished(6)",
          story.story_state(st4.props, 1003) == sm.TASK_STATE_FINISHED)

    print("\n[7] start_story_task：OP_ADD(Init) + OP_CHANGE(Doing) 两条")
    st5 = fresh("sm_e")
    # ★ 1000 会被 init_new_player 自动设为 Doing，所以这里用 1003
    ch = story.start_story_task(st5, 1003, NOW)
    check("返回两条描述", len(ch) == 2, len(ch))
    check("第一条是 add", bool(ch) and ch[0][0] == "add", ch[:1])
    check("第二条是 sub", len(ch) > 1 and ch[1][0] == "sub", ch[1:])
    check("最终状态 Doing(3)",
          story.story_state(st5.props, 1003) == sm.TASK_STATE_DOING)
    check("已 Doing 再 start 返回空", story.start_story_task(st5, 1003, NOW) == [])

    print("\n[8] 帧序列：add->OP_ADD, sub->OP_CHANGE")
    frames = _story_sync_frames(st5, ch)
    check("两条帧", len(frames) == 2, len(frames))
    ops = []
    for f in frames:
        fl = msmod.pb_parse(bytes(f[6:]))
        ops.append(fl.get(3, [None])[0])
    check("optype 依次是 OP_ADD(2) / OP_CHANGE(1)",
          ops == [msmod.OP_ADD, msmod.OP_CHANGE], ops)

    print("\n[9] 帧在客户端影子缓存里不崩")
    st6 = fresh("sm_f")
    pre = pre_snapshot(uid_of("sm_f"))
    frames6 = _story_sync_frames(st6, story.start_story_task(st6, 1003, NOW))
    sc, errs = frames_safe(pre, frames6, "start_story_task")
    check("帧序列在客户端不崩", not errs, errs[:2])
    got = sc_state(sc, 1003)
    check("影子缓存里 1003 的状态 = Doing(3)", got == sm.TASK_STATE_DOING, got)

    print("\n[10] 完成路径帧序列：OP_CHANGE(→6) / OP_ADD(done) / OP_DEL(bag)")
    st7 = fresh("sm_g")
    story.start_story_task(st7, 1000, NOW)
    pre7 = pre_snapshot(uid_of("sm_g"))
    ch_mark = story.mark_story_task_finished(st7, 1000, NOW)
    ch_arch = story.archive_story_task(st7, 1000, NOW)
    frames7 = _story_sync_frames(st7, ch_mark + ch_arch)
    bag_id = st7.props["story_task_bag"].get("__id__")
    done_id = st7.props["done_story_tasks"].get("__id__")
    ops7 = []
    for f in frames7:
        fl = msmod.pb_parse(bytes(f[6:]))
        ops7.append((fl.get(2, [None])[0], fl.get(3, [None])[0]))
    check("第一条是 bag 的 OP_CHANGE", bool(ops7) and ops7[0] == (bag_id, msmod.OP_CHANGE), ops7[:2])
    check("第二条是 done 的 OP_ADD",
          len(ops7) > 1 and ops7[1] == (done_id, msmod.OP_ADD), ops7[:2])
    check("第三条是 bag 的 OP_DEL",
          len(ops7) > 2 and ops7[2] == (bag_id, msmod.OP_DEL), ops7[:3])
    sc7, errs7 = frames_safe(pre7, frames7, "finish")
    check("完成路径的帧在客户端不崩", not errs7, errs7[:2])
    d7 = sc_state(sc7, 1000, "done_story_tasks")
    check("影子缓存里 1000 已在 done 且 state=6", d7 == sm.TASK_STATE_FINISHED, d7)

    print('\n[11] AST：全仓只有 state_machine.py 改 story_task_bag[*]["state"]')
    # 只扫 story 包（别的包写的是 TaskItem / ActiveTask，不是 StoryTaskItem）
    # ALLOWED_FUNCS 里的函数写的是 ActiveTask 的 state，不是 StoryTaskItem 的
    ALLOWED_FUNCS = {"make_active_task"}
    offenders = []
    for p in glob.glob(os.path.join(ROOT, "game_server", "story", "**", "*.py"),
                       recursive=True):
        if os.path.basename(p) == "state_machine.py" or "__pycache__" in p:
            continue
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in ALLOWED_FUNCS:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "state"):
                        offenders.append("%s:%d(%s)" % (
                            os.path.relpath(p, ROOT), node.lineno, fn.name))
    check("没有其它地方直接改 state 字段", not offenders, offenders)

    print("\n[12] finish_story_task 兼容封装")
    st8 = fresh("sm_h")
    story.start_story_task(st8, 1000, NOW)
    kinds8 = [c[0] for c in story.finish_story_task(st8, 1000, NOW)]
    check("含 sub", "sub" in kinds8, kinds8)
    check("含 add", "add" in kinds8, kinds8)
    check("含 del", "del" in kinds8, kinds8)
    check("最终在 done 里",
          1000 in st8.props["done_story_tasks"]
          and 1000 not in st8.props["story_task_bag"])
    st9 = fresh("sm_i")
    kinds9 = [c[0] for c in story.finish_story_task(st9, 1003, NOW)]
    check("不在包里的任务：add -> sub -> ... -> del",
          kinds9[:2] == ["add", "sub"] and "del" in kinds9, kinds9)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
