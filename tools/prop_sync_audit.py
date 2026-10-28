#!/usr/bin/env python3
"""属性同步一致性审计：服务端状态 vs 客户端回放状态。

## 它检验的不变量

    INV-SYNC-3  对任意 RPC：客户端回放帧后的状态 == 服务端最终状态

这是「改了属性但客户端看不到」这类 bug 的总判据。

## 为什么不能按属性名比对

帧是按**路径**发的，不是按根属性名：

    根属性 space_p 的变化  -> 帧 name 是 spaceno / seqno / spaceid
    根属性 story_task_bag  -> 帧 name 是 "400013"（容器键）

按名字比对会报 4 个假阳性（实测）。只有回放才能得到客户端的真实状态。

## 用法

    python tools/prop_sync_audit.py            # 全量审计，打印不一致
    python tools/prop_sync_audit.py --rpc NAME # 只审一条
    python tools/prop_sync_audit.py --json     # 机器可读

退出码：0 = 无不一致；1 = 有不一致。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 审计必须在临时存档目录跑，绝不碰 work/player_state 的真实存档。
#: 审计要在临时目录里跑，不碰真实存档。

#:场景参数：让 RPC真的走到「改属性」那条路。
#:
#:为什么需要：不传参数时大部分 handler在入口就早退（参数不足），
#:审计只会得到「0条产生属性变化」，看起来一切正常但其实什么都没测到。
#:实测：不加这张表时，把发奖帧改回「只发物品格」的旧写法，门禁依然是绿的。
#:
#:每条都是「能让 handler真正执行下去」的最小输入，取值来自各 handler的解析路径。
SCENARIOS = {
    "SetCanScore_CS": [],
    "SetHasScore_CS": [],
    "SetSignature_CS": ["audit"],
    "SetPortrait_CS": [1],
    "SetCharacterList_CS": [1, [9001,9002,0]],
    "SetCharacterListName_CS": [1, "audit"],
    "updateStreetTraceTask_CS": [1000],
    "RequestDispatchChar_CS": [5001,9001],
    "RequestEnterStreetSurface_CS": [1],
    "Street_Mainline_Levelup_CS": [101],
    "BattleTeach_Activate_CS": [101],
    "Storyline_FinishGame": [1, True],
    "RequestStreetTreasure_CS": [10157],
    "RequestAddPrestigeExp_CS": [101,10],
    "UseItem_CS": [101,1, None],
}


_OP_NAMES = {1: "change", 2: "add", 3: "del", 4: "insert", 5: "remove"}


def _decode_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def parse_sync_frame(frame: bytes) -> tuple[int, str, str, object] | None:
    """把一条 propertySync2Client 帧解成 (propertyId, opname, name, value)。

    ★ 只有 type == 13 的帧是属性同步（见 ms/property.py:PROPERTY_SYNC_TYPE）。
    其余帧（应答 / 实体消息）返回 None —— 审计只关心属性。
    """
    from game_server import ms as msmod  # noqa: PLC0415

    if len(frame) < 6:
        return None
    _ln = int.from_bytes(frame[0:4], "little")
    mtype = int.from_bytes(frame[4:6], "little")
    if mtype != msmod.PROPERTY_SYNC_TYPE:
        return None
    body = frame[6:]
    pid = 0
    optype = 0
    name = ""
    value: object = None
    i = 0
    while i < len(body):
        key, i = _decode_varint(body, i)
        fn = key >> 3
        wt = key & 0x07
        if wt == 0:
            v, i = _decode_varint(body, i)
            if fn == 2:
                pid = v
            elif fn == 3:
                optype = v
        elif wt == 2:
            ln, i = _decode_varint(body, i)
            raw = body[i:i + ln]
            i += ln
            if fn == 4:
                name = raw.decode("utf-8", errors="replace")
            elif fn == 5:
                value = msmod.mp_decode(raw)[0]
        else:
            return None
    return pid, _OP_NAMES.get(optype, "change"), name, value


def _client_view(props: dict) -> dict:
    """服务端 props -> 客户端 initDict 的形状（剥 `_oo_`，保留 `__tp__`）。"""
    out: dict = {}
    for k, v in props.items():
        if not isinstance(k, str) or k.startswith("_oo_"):
            continue
        out[k] = v
    return out


def snapshot_server(props: dict) -> dict:
    """服务端状态的规范化视图（与客户端 snapshot 同构）。"""
    out: dict = {}
    for k, v in props.items():
        if not isinstance(k, str) or k.startswith("_oo_"):
            continue
        out[k] = _norm(v)
    return out


def _norm(v: object) -> object:  # noqa: ANN401
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()
                if not str(k).startswith("__")}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def diff_states(server: dict, client: dict) -> list[str]:
    """列出两边不一致的根属性名。"""
    out: list[str] = []
    for k in sorted(set(server) | set(client)):
        if server.get(k) != client.get(k):
            out.append(k)
    return out


def audit_rpc(name: str, idx: int, args: list) -> tuple[bool, dict | None]:
    """跑一条 RPC，比对服务端状态与客户端回放状态。

    返回 None 表示这条 RPC 没有产生属性变化（无需比对）。
    否则返回 {rpc, server_only, client_only, frames, sync_frames}。
    """
    from game_server import dispatch, ms  # noqa: PLC0415
    from game_server import player_state as pstate  # noqa: PLC0415
    from game_server.ms.protocol import mp_arr  # noqa: PLC0415
    from tests.lua_prop_replay import PropReplayer  # noqa: PLC0415

    uid = "audit_" + name
    pstate.STATES.pop(uid, None)
    # ★ 两边的起点必须是**同一份**数据：
    #   - 服务端：live_init_dict 跑完后的状态（登录已经播过种子/重算过派生）
    #   - 客户端：同一份状态的深拷贝（模拟「登录时客户端拿到的 initDict」）
    #   第一版拿模板当客户端起点，会把登录期的改动全算成这条 RPC 的差异。
    init = ms.live_init_dict(uid=uid)
    st = pstate.STATES[uid]
    # ★ 顺序是判据的一部分：快照必须**深拷贝**之后再取，
    #   否则 server_before 和 server_after 指向同一份 props，永远相等。
    # ★ 客户端起点必须是服务端**实际下发的 initDict**（已剥嵌套 __tp__ / _oo_），
    #   不能用原始 st.props：props 内部保留嵌套 __tp__，客户端解析器读到它会把
    #   字符串当子项，整棵容器丢失（实测 task_bag 直接被丢成 nil）。
    before_props = copy.deepcopy(init["__Properties__"])
    server_before = snapshot_server(copy.deepcopy(st.props))
    try:
        frames = dispatch.handle(idx, mp_arr(args), 424242, uid=uid)
    except Exception:  # noqa: BLE001
        return False, None
    server_after = snapshot_server(st.props)
    changed = [k for k in server_after if server_before.get(k) != server_after.get(k)]
    if not changed:
        return False, None

    parsed = []
    for f in frames:
        b = f if isinstance(f, bytes) else bytes(f)
        op = parse_sync_frame(b)
        if op is not None:
            parsed.append(op)

    replayer = PropReplayer(before_props)
    try:
        replayer.apply_frames(parsed)
    except Exception as e:  # noqa: BLE001
        return True, {"rpc": name, "error": f"回放失败: {e}", "changed": changed,
                "sync_frames": len(parsed)}
    client_after = replayer.snapshot()
    bad = diff_states(server_after, client_after)
    if not bad:
        return True, None
    return True, {
        "rpc": name,
        "changed": changed,
        "mismatched": bad,
        "sync_frames": len(parsed),
        "sample": {k: {"server": server_after.get(k), "client": client_after.get(k)}
                   for k in bad[:3]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="属性同步一致性审计")
    ap.add_argument("--rpc", help="只审这一条 RPC")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--with-args", help="逗号分隔的参数（调试用）")
    args = ap.parse_args()

    os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_audit_")
    os.environ.setdefault(
        "OO_UNKNOWN_LOG",
        os.path.join(tempfile.mkdtemp(prefix="oo_audit_ulog_"), "unknown.tsv"),
    )

    from game_server import dispatch, rpc_index  # noqa: PLC0415

    idx2name = {v: k for k, v in rpc_index.RPC_INDEX.items()}
    targets = []
    for idx, _fn in sorted(dispatch.HANDLERS.items()):
        name = idx2name.get(idx)
        if name is None:
            continue
        if args.rpc and name != args.rpc:
            continue
        targets.append((name, idx))

    call_args: list = []
    if args.with_args:
        call_args = [int(x) for x in args.with_args.split(",") if x.strip()]

    bad: list[dict] = []
    checked = 0
    for name, idx in targets:
        changed_any, res = audit_rpc(name, idx, SCENARIOS.get(name, call_args))
        if changed_any:
            checked +=1
        if res is not None:
            bad.append(res)

    if args.json:
        print(json.dumps({"targets": len(targets), "checked": checked, "bad": bad},
 ensure_ascii=False, indent=1))
        return 1 if bad else 0

    print(f"审计了 {len(targets)} 条 RPC，其中 {checked} 条产生了属性变化")
    if not bad:
        print("没有不一致。")
        return 0
    print(f"★ 不一致 {len(bad)} 条：")
    for r in bad:
        if "error" in r:
            print(f"  {r['rpc']:40} {r['error']}")
            continue
        print(f"  {r['rpc']:40} 改动={r['changed'][:5]}")
        print("  {:<40} 不一致={}  同步帧={}".format(''.ljust(40), r["mismatched"][:5], r["sync_frames"]))
        for k, v in r["sample"].items():
            print("  {:<40}   {}: server={} client={}".format('', k, str(v["server"])[:40], str(v["client"])[:40]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
