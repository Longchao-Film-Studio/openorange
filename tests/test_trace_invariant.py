#!/usr/bin/env python
"""剧情任务「追踪目标」不变量测试（R1：把不变量写成能失败的断言）。

背景（2026-10-25 实机事故）：奶茶店主线打完后 NPC 带着继续推，走到「进龙潮阁」
那一步剧情死活触发不了；左上角显示的是**已经做完的** 1044「接到杜老爷消息」；
退出重登后立刻就能触发。

根因：`street_trace_taskid` 有三个写入点、三套规则（详见 game_server/story/trace.py
文件头），标点可以停在已完成任务上、也可以被换成无关任务。现已统一到
`story.trace.apply()`。

本测试锁死两条不变量：

  INV-1（auto 路径）：street_trace_taskid 要么 0，要么任务 state==Doing 且
                      GuideSceneId 为空或 == 当前 street_id。
  INV-2（manual 路径）：要么 0，要么恰好等于玩家点名追踪的那个任务。

外加：已完成任务绝不可能成为追踪目标；过期的手动意图会被清 0；
无论值变不变都推 OP_CHANGE 帧（客户端靠它刷新标点）。

用法: python tests/test_trace_invariant.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_trinv_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story import seed as SD  # noqa: E402
from game_server.story import trace as TR  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

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
    u = f"{name}_{_TAG}"
    msmod._live_init_dict(u)
    st = pstate.get_state(uid=u)
    SD.seed(st, NOW)
    return u, st


def auto_frame_names(frames):
    """从帧里提取 (name, op) —— 粗粒度够用：OP_CHANGE 帧名在 payload 里。"""
    names = []
    for f in frames or []:
        if isinstance(f, (bytes, bytearray)):
            try:
                s = bytes(f).decode("utf-8", "replace")
            except Exception:
                continue
            names.append(s)
    return names


def assert_inv1(tag, st, street_id=None):
    """INV-1：auto 路径写入的追踪目标必须「正在做 + 本街区」。"""
    tid = TR._as_int(st.props.get(TR.TRACE_KEY), 0) or 0
    if tid == 0:
        # 为 0 必须是「真的没有候选」，不能是随手返回 0 —— 否则这条断言会红
        sid = TR._street_of(st, street_id)
        items = TR._bag_items(st)
        cands = [t for t, it in items.items()
                 if TR._is_doing(it) and TR._scene_ok(t, sid)]
        check(f"{tag}：为 0 时确实没有可用候选", not cands, cands)
        return
    sid = TR._street_of(st, street_id)
    items = TR._bag_items(st)
    item = items.get(tid)
    ok_doing = bool(item) and TR._is_doing(item)
    ok_scene = TR._scene_ok(tid, sid)
    check(f"{tag}：{tid} 正在做", ok_doing,
          (item or {}).get("state"))
    check(f"{tag}：{tid} 属于街区 {sid}", ok_scene,
          (gamedata.story_task(tid) or {}).get("GuideSceneId"))


def run():
    global _TAG
    _TAG = f"{random.randint(0, 0xFFFF):04x}"
    del passed[:]
    del failed[:]

    print()
    print("[1] INV-1：切街区后追踪目标必须「正在做 + 本街区」")
    u1, st1 = fresh("inv1")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"], msmod.mp_arr([101]), 1, uid=u1)
    guarded_handle(rpc_index.RPC_INDEX["RequestSwitchStreet_CS"], msmod.mp_arr([100]), 2, uid=u1)
    assert_inv1("切到 100", st1, street_id=100)

    print()
    print("[2] ★ 回归 1044 事故：已完成任务绝不能被当成追踪目标")
    _u2, st2 = fresh("inv2")
    # 造一个「已完成」的 1044（GuideSceneId=101，与中浦区一致 —— 旧代码就是被它坑的）
    bag = st2.props.setdefault("story_task_bag", {})
    bag[1044] = {"__id__": 991001, "id": 1044, "state": 6}
    done = st2.props.setdefault("done_story_tasks", {})
    done[1044] = NOW
    # 玩家意图还停在 1044（旧代码会一直把它当成追踪目标）
    st2.props[TR.WANTED_KEY] = 1044
    st2.props[TR.TRACE_KEY] = 1044
    from game_server.story.trace import apply as trace_apply
    frames = []
    tid = trace_apply(st2, frames, "回归：完成任务不得被追踪", street_id=101)
    check("★ 追踪目标不再是已完成的 1044", tid != 1044, tid)
    check("★ 过期的手动意图被清 0",
          (st2.props.get(TR.WANTED_KEY) or 0) == 0,
          st2.props.get(TR.WANTED_KEY))
    check("★ 即使值没变也要推 OP_CHANGE 帧", len(frames) == 1, len(frames))

    print()
    print("[3] INV-2：手动追踪只认玩家点名的那个任务")
    u3, st3 = fresh("inv3")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"], msmod.mp_arr([101]), 1, uid=u3)
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u3)
    check("同街区手动追踪 1045 -> 生效",
          st3.props.get(TR.TRACE_KEY) == 1045, st3.props.get(TR.TRACE_KEY))
    # 跨街区：点名 1045 但在街区 100 -> 0，且不回退到别的任务
    u4, st4 = fresh("inv4")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"], msmod.mp_arr([100]), 1, uid=u4)
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u4)
    check("★ 跨街区点名 -> 0（不回退到无关任务）",
          (st4.props.get(TR.TRACE_KEY) or 0) == 0, st4.props.get(TR.TRACE_KEY))
    check("★ 但玩家意图保留（走到 101 会自动恢复）",
          st4.props.get(TR.WANTED_KEY) == 1045, st4.props.get(TR.WANTED_KEY))

    print()
    print("[4] INV-2 续：切到目标街区后自动恢复玩家点名的任务")
    guarded_handle(rpc_index.RPC_INDEX["RequestSwitchStreet_CS"],
                   msmod.mp_arr([101]), 3, uid=u4)
    check("★ 到街区 101 后恢复追踪 1045",
          st4.props.get(TR.TRACE_KEY) == 1045, st4.props.get(TR.TRACE_KEY))

    print()
    print("[5] 取消追踪（客户端发 -1000 哨兵）-> 0")
    u5, st5 = fresh("inv5")
    guarded_handle(rpc_index.RPC_INDEX["RpcEnterStreet"], msmod.mp_arr([101]), 1, uid=u5)
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([1045]), 2, uid=u5)
    guarded_handle(rpc_index.RPC_INDEX["updateStreetTraceTask_CS"],
                   msmod.mp_arr([-1000]), 3, uid=u5)
    check("★ 取消后追踪目标 = 0",
          (st5.props.get(TR.TRACE_KEY) or 0) == 0, st5.props.get(TR.TRACE_KEY))
    check("★ 取消后意图也清 0",
          (st5.props.get(TR.WANTED_KEY) or 0) == 0, st5.props.get(TR.WANTED_KEY))

    print()
    print("[6] resolve() 在各种空状态下都返回 0（不抛异常）")
    _u6, st6 = fresh("inv6")
    st6.props["story_task_bag"] = {}
    check("空任务包 -> 0", TR.resolve(st6) == 0, TR.resolve(st6))
    st6.props.pop("story_task_bag", None)
    check("没有任务包 -> 0", TR.resolve(st6) == 0, TR.resolve(st6))

    print()
    print("=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
