#!/usr/bin/env python
"""属性同步一致性：客户端回放帧后必须与服务端状态一致（INV-SYNC-3）。

## 这个测试要防的是什么

实机 bug：点面板「领取」→ 经验不涨 → 必须重新登录才看到。
根因不是「算错了」，而是**改了属性却没发同步帧**。

这类 bug 一共有 6 处（见 docs/报告-属性同步缺陷.md），修完必须有东西守着。

## 判据为什么是「回放」而不是「比对属性名」

帧是按路径发的：

    根属性 space_p        -> 帧的 name 是 spaceno / seqno / spaceid
    根属性 story_task_bag -> 帧的 name 是 "400013"（容器键）

按名字比对会误报（实测 4 例）。只有让客户端自己的解析器回放，
才知道客户端最终看到什么 —— 这才是真正要保证的不变量。

## 用例

  [1] 回放器本身可用：应用一帧后客户端状态跟着变
  [2] ★ 领头衔任务奖励：exp 必须进帧（本次实机 bug 的回归用例）
  [3] ★ 通行证经验道具：bp_info 的子字段必须进帧
  [4] 发奖路径统一走 Tx：不存在第二条手工拼帧的发奖出口

用法: python -m tests.test_prop_sync_consistency
"""
import copy
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_sync_")
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(os.environ["OO_STATE_DIR"], "unknown.tsv"),
)

_ok = 0
_bad: list[str] = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _bad.append(name)
        print(f"  FAIL  {name}" + (f" | {detail}" if detail else ""))


def _fresh(uid):
    """建一个登录后的状态，返回 (initDict 的 props, st)。"""
    from game_server import ms  # noqa: PLC0415
    from game_server import player_state as pstate  # noqa: PLC0415

    pstate.STATES.pop(uid, None)
    init = ms.live_init_dict(uid=uid)
    return copy.deepcopy(init["__Properties__"]), pstate.STATES[uid]


def _replay(before, frames):
    """把帧回放进客户端解析器，返回客户端最终状态。"""
    from tests.lua_prop_replay import PropReplayer  # noqa: PLC0415
    from tools.prop_sync_audit import parse_sync_frame  # noqa: PLC0415

    parsed = []
    for f in frames:
        b = f if isinstance(f, bytes) else bytes(f)
        op = parse_sync_frame(b)
        if op is not None:
            parsed.append(op)
    rp = PropReplayer(before)
    rp.apply_frames(parsed)
    return rp.snapshot(), parsed


def run():
    from game_server import dispatch, gamedata, ms, rpc_index  # noqa: PLC0415
    from game_server.ms.protocol import mp_arr  # noqa: PLC0415
    from tools.prop_sync_audit import diff_states, snapshot_server  # noqa: PLC0415

    print("\n[1] 回放器可用：应用一帧后客户端状态跟着变")
    before, _st = _fresh("sync_basic")
    client, parsed = _replay(before, [ms.build_property_sync("", 0, ms.OP_CHANGE, "level", 5)])
    check("change 帧被回放", parsed == [(0, "change", "level", 5)], parsed)
    check("客户端读到新值", client.get("level") == 5, client.get("level"))

    print("\n[2] ★ 领头衔任务奖励：exp 必须进帧")
    before, st = _fresh("sync_claim")
    slot = (st.props.get("task_bag") or {}).get(10) or {}
    cand = None
    for tid in [k for k in slot if isinstance(k, int)]:
        aw = (gamedata.task(tid) or {}).get("AwardItemList") or {}
        if aw.get("2") or aw.get(2):
            cand = tid
            break
    check("找到一条带经验的挑战任务", cand is not None, cand)
    if cand is not None:
        slot[cand]["state"] = 4  # WaitDone
        st.set("task_bag", st.props.get("task_bag"))
        before2 = copy.deepcopy(before)
        before2["task_bag"] = copy.deepcopy(  # type: ignore[assignment]
            __import__("json").loads(__import__("json").dumps(
                {str(k): v for k, v in st.props["task_bag"].items()},
                default=str)))
        exp_before = st.props.get("exp")
        frames = dispatch.handle(
            rpc_index.RPC_INDEX["RequestDoneTitleTask_CS"],
            mp_arr([[cand], -1]), 1, uid="sync_claim")
        exp_after = st.props.get("exp")
        check(f"服务端 exp 增加了（{exp_before} -> {exp_after}）",
              exp_after > exp_before, exp_after)
        client, parsed = _replay(before, frames)
        exp_names = [p for p in parsed if p[2] == "exp"]
        check("★ 帧里包含 exp 的同步", bool(exp_names), [p[2] for p in parsed])
        check("★ 客户端 exp 与服务端一致",
              client.get("exp") == exp_after,
              f"client={client.get('exp')} server={exp_after}")

    print("\n[3] ★ 通行证经验道具：bp_info 子字段必须进帧")
    before, st = _fresh("sync_bp")
    items = gamedata.table("ItemData") or {}
    rows = list(items.values()) if isinstance(items, dict) else list(items)
    bp = [x for x in rows if isinstance(x, dict) and x.get("autoUse")
          and x.get("useType") == "avatarAttr"
          and "BpExp" in ((x.get("useParam") or {}).get("KeyList") or [])]
    check("找到通行证经验道具", bool(bp), len(bp))
    if bp:
        from game_server.property.bag import grants  # noqa: PLC0415

        iid = int(bp[0]["itemId"])
        tx, _ocs = grants.grant_many(st.props, {iid: 1})
        client, parsed = _replay(before, tx.frames())
        sent = {p[2] for p in parsed}
        check("★ bp_exp 进了帧", "bp_exp" in sent, sent)
        check("★ all_bp_exp 进了帧", "all_bp_exp" in sent, sent)
        server = snapshot_server(st.props)
        check("★ 客户端 bp_info 与服务端一致",
              not diff_states(server, client),
              diff_states(server, client)[:5])

    print("\n[4] 发奖路径统一走 Tx：不存在第二条手工拼帧的发奖出口")
    import inspect  # noqa: PLC0415

    from game_server import (
        level,  # noqa: PLC0415
        )
    from game_server.dungeon import reward as reward_mod  # noqa: PLC0415
    from game_server.street import treasure  # noqa: PLC0415

    for mod, label in (
        (reward_mod, "dungeon/reward.py"),
        (treasure, "street/treasure.py"),
        (level, "level.py"),
    ):
        src = inspect.getsource(mod)
        check(f"{label} 不再调用 grants.sync_frames",
              "sync_frames(" not in src)

    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(run())
