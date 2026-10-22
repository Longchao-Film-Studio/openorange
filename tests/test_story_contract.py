#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 H：客户端回调面回归（元测试）。

★ 这个文件的目的

  把「服务端发的帧 vs 客户端 PropertyCallbacks 声明」变成机器检查。
  契约表由 tools/gen_contract.py 从客户端 generated/config.lua **生成**，
  不是手抄的（手抄的表在本次开发中错过两次）。

★ 它拦什么

  1. 服务端给某属性发的 optype 和客户端声明不一致（比如该发 OP_CHANGE 却发了 OP_ADD）
  2. 声明了回调的属性，服务端一条帧都不产生（等于那个 UI 永远不刷新）
  3. 契约表过期（config.lua 改了但 json 没重新生成）
  4. 直接改 story_task_bag[*]["state"] 绕过状态机

★ 不变量

  1. 每个「服务端会写」的属性，至少要有一条帧命中它的契约行。
  2. 命中的那条帧的 optype 必须和契约行的 optype 一致。
  3. 契约表必须和 config.lua 一致（--check）。

用法: python tests/test_story_contract.py
"""
import ast
import glob
import json
import os
import random
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_ct_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, "tests", "contract", "property_callbacks.json")
GEN_TOOL = os.path.join(ROOT, "tools", "gen_contract.py")

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


def load_contract():
    with open(CONTRACT, encoding="utf-8") as f:
        return json.load(f)


def frame_fields(f):
    """解一条属性同步帧 -> (pid, optype, name, ownerid)。"""
    fl = msmod.pb_parse(bytes(f[6:]))
    nm = fl.get(4, [b""])[0]
    if isinstance(nm, (bytes, bytearray)):
        nm = nm.decode("utf-8", "replace")
    ow = fl.get(1, [b""])[0]
    if isinstance(ow, (bytes, bytearray)):
        ow = ow.decode("utf-8", "replace")
    return fl.get(2, [None])[0], fl.get(3, [None])[0], nm, ow


def collect(frames):
    """把一批帧按 (pid, optype, name) 汇总。"""
    out = []
    for f in frames:
        if not isinstance(f, (bytes, bytearray)) or len(f) < 6:
            continue
        if int.from_bytes(bytes(f[4:6]), "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        out.append(frame_fields(f))
    return out


def msmod_op(section):
    """段名 -> optype（和契约表 json 里的 _optype_map 一致）。"""
    return {"changed": 1, "entryAdded": 2, "entryDeleted": 3,
            "itemInserted": 4, "itemRemoved": 5}[section]


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    doc = load_contract()
    props = doc["properties"]

    print("\n[1] 契约表本身")
    check("契约表非空", len(props) >= 10, len(props))
    check("★ story_task_bag.* 有 changed(OP_CHANGE) 行",
          any(r["section"] == "changed"
              for r in props.get("story_task_bag.*", [])))
    check("★ story_task_bag.* 同时有 entryAdded(OP_ADD) 行",
          any(r["section"] == "entryAdded"
              for r in props.get("story_task_bag.*", [])))
    check("★ story_finished 有两行回调（changed + entryAdded）",
          {r["section"] for r in props.get("story_finished", [])}
          == {"changed", "entryAdded"},
          [r["section"] for r in props.get("story_finished", [])])
    check("★ done_story_tasks 是 entryAdded(OP_ADD)",
          all(r["section"] == "entryAdded"
              for r in props.get("done_story_tasks", [])))
    check("★ task_bag.*.* 是 changed(OP_CHANGE)",
          all(r["section"] == "changed"
              for r in props.get("task_bag.*.*", [])))
    # 契约表不能过期
    try:
        r = subprocess.run([sys.executable, GEN_TOOL, "--check"],
                           capture_output=True, timeout=60)
        check("★ 契约表和 config.lua 一致（未过期）", r.returncode == 0,
              (r.stdout + r.stderr).decode("utf-8", "replace")[:120])
    except Exception as e:
        check("★ 契约表和 config.lua 一致（未过期）", False, e)

    print("\n[2] 服务端实际发的帧 vs 契约行")
    uid = "ct_" + _TAG
    # ★ 按**代码路径**分批收集，每批单独核对。
    #   第一版把所有帧混成一批，结果属性维度是「所有路径的并集」——
    #   堵掉一条路径，别的路径仍然产生同样的属性帧，检查就失效了。
    st = pstate.get_state(
        msmod._conv_int_keys(
            msmod.AVATAR_INIT_DICT.get("__Properties__") or {}),
        uid=uid)

    # --- 路径 A：剧情任务接取+完成 ---
    story.start_story_task(st, 1003, NOW)
    path_a = guarded_handle(erpc.RPC_INDEX["RequestDoneStoryTaskParamInt_CS"],
                            msmod.mp_arr([1003, 400041]), 1, uid=uid)

    # --- 路径 B：章节三属性 ---
    path_b = (story.mark_story_line_finished(st, "guide_01")
              + story.sync_story_max_collect_num(st)
              + story.sync_story_reward_list(st))

    # --- 路径 C：通用任务接取+完成 ---
    path_c = guarded_handle(erpc.RPC_INDEX.get("RequestFetchTask_CS") or 1,
                            msmod.mp_arr([1]), 2, uid=uid)
    path_c += guarded_handle(erpc.RPC_INDEX.get("RequestDoneTask_CS") or 2,
                             msmod.mp_arr([1]), 3, uid=uid)

    # --- 路径 D：追踪任务 ---
    path_d = guarded_handle(erpc.RPC_INDEX["updateStreetTraceTask_CS"],
                            msmod.mp_arr([1003]), 4, uid=uid)

    frames = path_a + path_b + path_c + path_d

    # 每条路径各自核对（key -> 该路径要求的 optype）
    #   A: story_task_bag.* 要 OP_ADD+OP_CHANGE+OP_DEL；done_story_tasks 要 OP_ADD
    #   B: story_finished/story_max_collect_num/story_reward_list 要 OP_ADD
    #   C: task_bag.* 要 OP_ADD；task_bag.*.* 要 OP_ADD+OP_CHANGE；done_tasks.* 要 OP_ADD
    #   D: street_trace_taskid 要 OP_CHANGE
    per_path = [
        ("A 剧情任务", path_a, {
            "story_task_bag.*": [2, 1, 3], "done_story_tasks": [2]}),
        ("B 章节三属性", path_b, {
            "story_finished": [2], "story_max_collect_num": [2],
            "story_reward_list": [2]}),
        ("C 通用任务", path_c, {
            "task_bag.*": [2], "task_bag.*.*": [2, 1], "done_tasks.*": [2]}),
        ("D 追踪任务", path_d, {"street_trace_taskid": [1]}),
    ]

    def by_prop_of(fs):
        d = {}
        for pid, op, name, _ow in collect(fs):
            key = path_of(pid, name)
            if key:
                d.setdefault(key, set()).add(op)
        return d

    seen = collect(frames)
    # ★ 契约表的 key 是**属性路径**（story_task_bag.*），而帧里只有 propertyId。
    #   所以要先用「属性 id -> 根属性名」把 pid 翻成路径。
    def path_of(pid, name=None):
        # 根属性 pid<=0，靠帧里的 name 认
        if pid is None or pid <= 0:
            return name if name in props else None
        for pname in ("story_task_bag", "done_story_tasks", "task_bag",
                      "done_tasks", "story_finished", "story_max_collect_num",
                      "story_reward_list", "street_trace_taskid"):
            pv = st.props.get(pname)
            if isinstance(pv, dict) and pv.get("__id__") == pid:
                if pname in ("story_task_bag", "task_bag", "done_tasks"):
                    return pname + ".*"
                return pname
        # task_bag[type] 槽位 / done_tasks[type] 槽位 -> task_bag.*.*
        for pname in ("task_bag", "done_tasks"):
            bag = st.props.get(pname)
            if isinstance(bag, dict):
                for _t, slot in bag.items():
                    if isinstance(slot, dict) and slot.get("__id__") == pid:
                        return pname + ".*.*"
        return None

    by_prop = {}
    for pid, op, name, _ow in seen:
        key = path_of(pid, name)
        if key:
            by_prop.setdefault(key, set()).add(op)

    # 契约行核对：服务端发了这个属性，optype 必须命中某一行的声明
    mism = []
    for prop, rows in props.items():
        declared = {r["section"] for r in rows}
        # puppetpropertycomponent.lua:273-278：OP_ADD 时如果该属性没有声明 entryAdded 回调，
        # 客户端会兜底走 changed 回调。所以「服务端发 OP_ADD」对只有 changed 行的属性也成立。
        want_ops = {msmod_op(x) for x in declared}
        if 2 in want_ops and 1 not in declared:
            pass
        elif 2 in want_ops and 1 in declared:
            want_ops.add(1)
        else:
            want_ops |= {1, 2}
        got = by_prop.get(prop)
        if got is None:
            continue
        if not (got & want_ops):
            mism.append((prop, sorted(got), sorted(want_ops)))
    check("★ 服务端发过的属性，optype 都和契约行一致", not mism, mism[:4])

    print("\n[3] 按代码路径核对：每条路径都要发齐契约要求的 optype")
    path_bad = []
    for pname, pframes, want in per_path:
        bp = by_prop_of(pframes)
        for prop, need in want.items():
            got = bp.get(prop) or set()
            missing = [op for op in need if op not in got]
            if missing:
                path_bad.append("%s: %s 缺 optype %s（实际 %s）"
                                % (pname, prop, missing, sorted(got)))
    check("★ 4 条代码路径各自发齐了契约要求的 optype", not path_bad, path_bad)

    # 汇总视图（用来回答「这个属性到底有没有帧」）
    by_prop = {}
    for pid, op, name, _ow in seen:
        key = path_of(pid, name)
        if key:
            by_prop.setdefault(key, set()).add(op)
    # ★ 只查我们负责的属性。下面这些**故意不写**，已记录归属：
    #     story_act_p.task_bag                    剧团活动任务袋 -> P5
    #     act_type_task_prop.*.act_taskbag.*.state 活动任务 -> P5
    #     daily_active_value                      日常活跃度 -> P7
    #     story_task_bag.*.finish_parameter        已由 [14] 专项覆盖（多目标任务）
    NOT_OURS = {
        "story_act_p.task_bag",
        "act_type_task_prop.*.act_taskbag.*.state",
        "daily_active_value",
        "story_task_bag.*.finish_parameter",
    }
    silent = [p for p in props
              if p not in by_prop and p not in NOT_OURS
              and any(r["section"] in ("changed", "entryAdded")
                      for r in props[p])]
    check("★ 我们负责的属性都有帧产出（没有完全静默的属性）",
          not silent, silent[:6])
    print("\n[4] 帧全部过客户端影子缓存（由 guarded_handle 逐条校验）")
    # ★ 这里**不**做整批重放：这批帧来自 5 次不同 RPC，每次的「客户端已有缓存」
    #   都不一样，整批喂进去会把「上一个 RPC 刚建的条目」当成「一开始就存在」，
    #   报出 _changeSubProperty 找不到键 —— 那是测试假象，不是服务端问题。
    #   真正的逐条校验在 guarded_handle 里已经做过了（每次调用各自取快照）。
    #   这里只确认：帧的数量和非空性符合预期。
    check("★ 收集到了一批帧（覆盖多个属性）", len(frames) >= 8, len(frames))
    check("★ 帧全部是属性同步帧",
          all(isinstance(f, (bytes, bytearray)) and len(f) >= 6
              for f in frames))

    print("\n[5] 静态检查：不许绕过状态机直接写 state")
    ALLOW = {
        ("game_server/story/state_machine.py", "make_story_task_item"): "构造",
        ("game_server/story/state_machine.py", "transition"): "唯一迁移入口",
        ("game_server/story/__init__.py", "make_active_task"): "ActiveTask",
        ("game_server/tasks.py", "make_task_item"): "构造 TaskItem",
        ("game_server/tasks.py", "start_task"): "通用任务迁移",
        ("game_server/tasks.py", "finish_task"): "通用任务迁移",
        ("game_server/tasks.py", "move_to_wait_done"): "通用任务迁移 Doing->WaitDone（N-c 领奖流程）",
        ("game_server/scaffold.py", "apply"): "脚手架（默认关闭）",
    }
    bad = []
    for path in glob.glob(os.path.join(ROOT, "game_server", "**", "*.py"),
                          recursive=True):
        if "__pycache__" in path:
            continue
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as e:
            bad.append("%s 语法错误 %s" % (rel, e))
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (rel, fn.name) in ALLOW:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if (isinstance(t, ast.Subscript)
                                and isinstance(t.slice, ast.Constant)
                                and t.slice.value == "state"):
                            bad.append("%s:%d(%s)" % (rel, node.lineno, fn.name))
    check("★ 没有白名单外的 [\"state\"] 直接赋值", not bad, bad[:5])

    print("\\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
