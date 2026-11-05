#!/usr/bin/env python3
"""帧可达性审计（INV-SYNC-4）：服务端发出的每条属性同步帧，客户端都必须有接收方。

## 为什么不变量要分成两条

    INV-SYNC-3（tools/prop_sync_audit.py）
        客户端回放帧后的状态 == 服务端最终状态
        抓的是「状态丢了」—— 帧根本没发、或者发错了值。

    INV-SYNC-4（本工具）
        每条帧的 optype + 路径，在客户端 PropertyCallbacks 里都能查到接收方
        抓的是「状态对但 UI 不刷新」—— 帧发了、值也对，
        但客户端按 optype 选表、用路径精确查表，查不到就**静默丢弃**。

两条都绿才算真同步。2026-11-05 的系统解锁事故只有 INV-SYNC-4 能抓：
帧发了、状态完全正确（INV-SYNC-3 绿），但 83 个系统入口的按钮都不出现，
因为 OP_CHANGE 算出的查表 key 是 system_p.unlocked_system.*，
而客户端只声明了 system_p.unlocked_system。

## 判据不在本文件

路径推算和声明表都在 game_server/property/callback_contract.py
（现场解析客户端 generated/config.lua，不手抄）。
本工具只负责「把所有 handler 都驱动一遍，把 ops 收上来喂给它」。

## 覆盖面

不追求每条 RPC 都产生帧（大部分 handler 参数不全会早退），
追求的是**每条会写属性的路径都被走到**。三种驱动方式：

  1. prop_sync_audit.SCENARIOS —— 人工挑过的最小可执行参数
  2. 通用参数形状 —— 对每个已注册 handler 试若干种，能产生 op 的都要
  3. 显式流程 —— 剧情播种/完成/领奖、等级、邮件、送礼，这些是主线必经之路

## 用法

    python tools/audit_frame_reachability.py            # 全量，打印结论
    python tools/audit_frame_reachability.py --json     # 机器可读
    python tools/audit_frame_reachability.py --verbose  # 列出每条驱动的 RPC

退出码：0 = 没有不可达帧；1 = 有。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 审计必须在临时存档目录跑，绝不碰真实存档。
_TMP = tempfile.mkdtemp(prefix="oo_frame_audit_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_TMP, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_TMP, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_TMP, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_TMP, "cap"))

#: 通用参数形状。handler 参数不全会早退，多试几种能覆盖更多分支。
ARG_SHAPES: list[list] = [
    [],
    [0],
    [1],
    ["audit"],
    [1, 1],
    [1, True],
    [{}],
    [[1, 2]],
    [1, {}],
    [1, 1, {}],
    [{"reqMs": 1}],
]


def _spy(tx_mod):
    """装一个 Tx.frames 的探针，把每次产出的 ops 收上来。"""
    ops: list = []
    orig = tx_mod.Tx.frames

    def spy(self, strict=False):
        ops.extend(self.ops)
        return orig(self, strict=strict)

    tx_mod.Tx.frames = spy
    return ops, lambda: setattr(tx_mod.Tx, "frames", orig)


def _drive_rpc(dispatch, msmod, ps, name: str, args: list, uid: str) -> None:
    idx = dispatch.RPC_INDEX.get(name)
    if idx is None:
        return
    ps.STATES.pop(uid, None)
    try:
        msmod.live_init_dict(uid=uid)
        dispatch.handle(idx, msmod.mp_arr(args), 424242, uid=uid)
    except Exception:  # noqa: BLE001 —— 审计不关心 handler 报错，只关心它产出的帧
        pass


def _flows(dispatch, msmod, ps, _gamedata):  # noqa: C901 —— 一条流程一段，平铺更好读
    """显式流程：主线必经之路，参数是确定的。"""
    from game_server import level as level_mod
    from game_server import story as story_mod
    from game_server.property.bag import grants
    from game_server.property.tx import Tx
    from game_server.story import events as events_mod
    from game_server.story.rpc import recalc_system_unlocks
    from game_server.story.state_machine import create_task, transition

    def st_of(uid):
        ps.STATES.pop(uid, None)
        msmod.live_init_dict(uid=uid)
        return ps.get_state(uid=uid)

    # 1) 剧情任务：播种 -> 完成 -> 领奖（解锁系统就在这条路上）
    st = st_of("flow_story")
    for tid in (1000, 1005, 1039, 1044):
        try:
            create_task(st, tid, 1700000000)
            transition(st, tid, story_mod.TASK_STATE_FINISHED, source="audit")
        except Exception:  # noqa: BLE001
            pass
    recalc_system_unlocks(st)

    # 2) 通用任务：接 -> 完成 -> 领
    from game_server import tasks as tasks_mod
    st2 = st_of("flow_task")
    for tid in (5101303, 5000005, 5101806):
        try:
            tasks_mod.start_task(st2, tid, 1700000000)
            tasks_mod.finish_task(st2, tid, 1700000000)
        except Exception:  # noqa: BLE001
            pass

    # 3) 等级：升级 + 领升级奖励
    st3 = st_of("flow_level")
    try:
        level_mod.upgrade(st3)
        level_mod.grant_level_award(st3)
    except Exception:  # noqa: BLE001
        pass

    # 4) 事件：把每个已实现的构造器都发一遍
    st4 = st_of("flow_event")
    ctors = [
        events_mod.ev_sign(),
        events_mod.ev_finish_task(1039),
        events_mod.ev_dungeon_finish(1004, 1, 1),
        events_mod.ev_consume(132, 100),
        events_mod.ev_gift_favor(1001, 1),
        events_mod.ev_favor_add_level(1001, 2),
        events_mod.ev_get_item(701, 1),
        events_mod.ev_submit_item(701, 1, 1, [701]),
        events_mod.ev_char_data(level=10),
        events_mod.ev_street_interact(1, 1),
    ]
    for ev in ctors:
        try:
            events_mod.apply_event(st4, ev)
        except Exception:  # noqa: BLE001
            pass

    # 5) 背包：发道具 + 用道具 + 送礼
    st5 = st_of("flow_bag")
    try:
        tx = Tx(st5.props)
        for iid in (2, 132, 715, 81001):
            grants.grant(st5.props, iid, 50, tx=tx, source="audit")
        st5.save()
        for iid in (2, 715, 81001):
            _drive_rpc(dispatch, msmod, ps, "UseItem_CS", [iid, 1, {}], "flow_bag")
        fav = st5.props.get("favor_exp_map")
        if isinstance(fav, dict):
            _drive_rpc(dispatch, msmod, ps, "AddCharFavorExp_CS",
                       [1001, {str(81001): 1}], "flow_bag")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from game_server import dispatch, gamedata, level  # noqa: E402,F401
    from game_server import ms as msmod
    from game_server import player_state as ps  # noqa: E402
    from game_server.property import callback_contract as cb  # noqa: E402
    from game_server.property import tx as tx_mod  # noqa: E402

    if not cb.available():
        print(f"[INV-SYNC-4] 跳过：找不到客户端 config.lua（{cb.config_lua_path()}）")
        return 0

    ops, restore = _spy(tx_mod)
    driven: list[str] = []
    try:
        # 1) 人工挑过的最小可执行参数
        from tools.prop_sync_audit import SCENARIOS  # noqa: PLC0415
        for name, a in SCENARIOS.items():
            before = len(ops)
            _drive_rpc(dispatch, msmod, ps, name, a, "sc_" + name)
            if len(ops) > before:
                driven.append(f"scenario:{name}")

        # 2) 通用参数形状，覆盖所有已注册 handler
        names = sorted(dispatch.RPC_INDEX)
        for name in names:
            for shape in ARG_SHAPES:
                before = len(ops)
                _drive_rpc(dispatch, msmod, ps, name, shape, "gen_" + name)
                if len(ops) > before:
                    driven.append(f"generic:{name}{shape!r}")
                    break

        # 3) 显式流程
        _flows(dispatch, msmod, ps, gamedata)
    finally:
        restore()

    bad = cb.unreachable(ops)
    summary = {
        "ops": len(ops),
        "driven": len(driven),
        "unreachable": len(bad),
        "by_optype": dict(collections.Counter(
            cb.OP_NAMES.get(o.optype, str(o.optype)) for o in ops)),
    }

    if args.json:
        print(json.dumps({
            "summary": summary,
            "unreachable": [
                {"optype": cb.OP_NAMES.get(o.optype, o.optype),
                 "path": [str(x) for x in o.path],
                 "key": k} for o, k in bad
            ],
        }, ensure_ascii=False, indent=1))
        return 0 if not bad else 1

    if args.verbose:
        print(f"驱动 {len(driven)} 次产生了属性变更：")
        for d in driven[:60]:
            print("  " + d)
        print()

    print("=" * 60)
    print("INV-SYNC-4 · 帧可达性")
    print("=" * 60)
    print(f"收集帧        : {summary['ops']}")
    print(f"产生变更的驱动: {summary['driven']}")
    print(f"按 optype     : {summary['by_optype']}")
    print()
    if not bad:
        print("每条帧客户端都有接收方。")
        return 0
    print(f"发现 {len(bad)} 条客户端收不到的帧：")
    seen = collections.Counter()
    for o, k in bad:
        seen[(cb.OP_NAMES.get(o.optype, str(o.optype)),
              ".".join(str(x) for x in o.path), k)] += 1
    for (opn, path, k), n in seen.most_common(40):
        print(f"  x{n:<3} {opn:12} {path:48} 查表 key={k}")
    print()
    print("修法：容器在客户端用精确路径声明了，新增子项就必须 OP_ADD")
    print("（先直接改 props 再 tx.set 会让 Tx 判成 OP_CHANGE）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
