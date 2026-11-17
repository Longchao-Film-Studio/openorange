#!/usr/bin/env python3
"""属性写入口审计（INV-PROPS-2）：业务路径上不许有绕过 Tx 的裸写。

## 为什么需要它

2026-11-17 实机：主线任务 1007「拍摄复盘」永远做不完。
查下来不是剧情逻辑 —— 是服务端自己把连接打断了：

    street/interact.py:52   ia[40006] = 400061        # 裸写嵌套属性
    -> PropsWriteError（B 组守卫）
    -> gate 的 except 直接进 finally 关连接
    -> 客户端 lose server，之后所有上报被丢弃
    -> 任务状态永远停在 Doing

守卫本身是对的（业务写入必须走 Tx），错的是**还有没改完的写入点**。
而测试抓不到：tools/run_all_tests.py 设了 OO_ALLOW_PROPS_WRITE=1，
守卫在整个测试进程里等于关闭。

## 判据（两层，缺一层都会被绕过去）

  1. report 取证：OO_GUARD_MODE=report 下驱动全部 handler + 显式流程，
     守卫不抛、只记录「哪一行写了哪个键」-> 拿到**完整清单**
  2. strict 门禁：不设 report 也不设 bypass，重跑一遍，
     必须**一次 PropsWriteError 都不出现**

## 覆盖方式

复用现有审计的驱动面（不自己造一套参数）：
  · tools.prop_sync_audit.SCENARIOS —— 人工挑过的最小可执行参数
  · tools.audit_frame_reachability.ARG_SHAPES —— 每个 handler 试若干形状
  · tools.audit_frame_reachability._flows —— 剧情/任务/等级/事件/背包五条流程

## 用法

    python tools/audit_props_writes.py            # 取证：列出全部裸写点
    python tools/audit_props_writes.py --strict   # 门禁：有裸写就非 0
    python tools/audit_props_writes.py --json

退出码：0 = 干净（或仅取证）；1 = 有裸写。
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
_TMP = tempfile.mkdtemp(prefix="oo_props_audit_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_TMP, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_TMP, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_TMP, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_TMP, "cap"))

# ★ 审计期间绝不能开着这两个后门，否则什么也测不到
os.environ.pop("OO_ALLOW_PROPS_WRITE", None)


def _note(msg: str) -> None:
    """诊断信息写 stderr —— stdout 只留 JSON，否则门禁解析不了。"""
    print(msg, file=sys.stderr)


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def _drive(dispatch, msmod, ps, name, args, uid, errors, fresh=True):
    """驱动一条 RPC；把严格模式下逃出来的 PropsWriteError 记下来。

    ★ fresh=False 时**不重建存档**，在上一轮留下的状态上再跑一遍。
      这一步是必须的：裸写只发生在「容器已经存在」的分支上 ——
      空存档走的是「新建容器」分支（普通 dict，写它不触发守卫），
      正好把 street_info[101] / character_list 这类真实存档里的容器全跳过。
      实测：只跑 fresh=True 时 street/interact.py:52 完全不报，
      而那一行正是 1007 卡死的原因。
    """
    from game_server.write_guard import PropsWriteError

    idx = dispatch.RPC_INDEX.get(name)
    if idx is None:
        return
    if fresh:
        ps.STATES.pop(uid, None)
        msmod.live_init_dict(uid=uid)
    try:
        dispatch.handle(idx, msmod.mp_arr(args), 424242, uid=uid)
    except PropsWriteError as e:
        errors.append((name, str(e)))
    except Exception:  # noqa: BLE001 —— 其它异常不是本审计的判据
        pass


#: 最近一次审计的受控容器覆盖（guarded / total）
_COVERAGE: dict[str, int] = {}


def _street_flows(dispatch, msmod, ps, errors) -> int:
    """街区真实流程：主线 1007 卡死就发生在这条链上，必须每次都驱动。

    ★ 为什么不能靠通用参数形状：Street_Actor_InteractID_Update_CS 需要 5 个参数，
      而且**必须在「street_info[101] 已存在」的状态下**跑，才会走到
      「改已有 inter_actor_info」那个分支 —— 空存档走的是新建分支（写普通 dict
      不报违规）。2026-11-17 的事故点正好在「已存在」那一侧。
    """
    from game_server.property.tx import Tx
    from game_server.street.info import _make_street_info_entry

    uid = "pw_flows"
    ps.STATES.pop(uid, None)
    msmod.live_init_dict(uid=uid)
    st = ps.get_state(uid=uid)
    tx = Tx(st.props)
    tx.set(("street_id",), 101)
    tx.set(("street_info", 101), _make_street_info_entry(st))
    st.save()

    n = 0
    for args in (
        [40006, 40006, 400061, 1007, 400061],   # 主线 1007「拍摄复盘」
        [40006, 40006, 400061, 0, 400061],      # 同一交互再点一次（条目已存在）
        [40004, 40004, 400041, 0, 400041],
    ):
        _drive(dispatch, msmod, ps, "Street_Actor_InteractID_Update_CS",
               args, uid, errors, fresh=False)
        n += 1
    for args in ([1, 1], [2, 1]):
        _drive(dispatch, msmod, ps, "Street_Redpoint_Update_CS",
               args, uid, errors, fresh=False)
        n += 1
    for name in ("Street_CD_Update_CS", "RequestTouchTreasure_CS",
                 "RequestStreetTreasure_CS", "RequestAddPrestigeExp_CS",
                 "RequestGetPrestigeAward_CS", "RequestGetPrestigeTaskAward_CS",
                 "RequestDispatchChar_CS", "RequestFinishDispatch_CS",
                 "RequestCancelDispatchChar_CS",
                 "RequestClearStreetTreasureActor_CS"):
        for args in ([101, 1], [101, 1, 1], []):
            _drive(dispatch, msmod, ps, name, args, uid, errors, fresh=False)
            n += 1
    return n


def _guard_coverage(props) -> tuple[int, int]:
    """数 props 树里「受控容器 / 容器总数」。

    ★ 为什么要这个指标：守卫只在受控容器上生效（GuardedDict.__setitem__）。
      运行期新建的普通 dict 子树不受管辖 —— 那个洞不能被当成不存在。
      指标下降 = 新代码又往树里塞了普通 dict，等于给裸写开后门。
    """
    from game_server.write_guard import GuardedDict, GuardedList

    total = guarded = 0
    seen: set[int] = set()
    stack = [props]
    while stack:
        v = stack.pop()
        if id(v) in seen:
            continue
        seen.add(id(v))
        if isinstance(v, dict):
            total += 1
            if isinstance(v, GuardedDict):
                guarded += 1
            stack.extend(v.values())
        elif isinstance(v, list):
            total += 1
            if isinstance(v, GuardedList):
                guarded += 1
            stack.extend(v)
    return guarded, total


def run(*, strict: bool, fast: bool = False):
    from game_server import dispatch, write_guard
    from game_server import ms as msmod
    from game_server import player_state as ps
    from tools import audit_frame_reachability as reach

    if not strict:
        os.environ["OO_GUARD_MODE"] = "report"
    write_guard.reset_violations()

    errors: list[tuple[str, str]] = []
    driven = 0

    from tools.prop_sync_audit import SCENARIOS

    for name, a in SCENARIOS.items():
        for fresh in (True, False):
            _drive(dispatch, msmod, ps, name, a, "pw_sc_" + name, errors, fresh)
            driven += 1

    if not fast:
        for name in sorted(dispatch.RPC_INDEX):
            for shape in reach.ARG_SHAPES:
                for fresh in (True, False):
                    _drive(dispatch, msmod, ps, name, shape, "pw_gen_" + name,
                           errors, fresh)
                    driven += 1

    try:
        reach._flows(dispatch, msmod, ps, None)
    except Exception as e:  # noqa: BLE001 —— 流程失败本身由别的审计负责
        _note(f"（显式流程中断：{e}）")

    try:
        driven += _street_flows(dispatch, msmod, ps, errors)
    except Exception as e:  # noqa: BLE001
        _note(f"（街区流程中断：{e}）")

    guarded = total = 0
    for st in list(ps.STATES.values()):
        g, t = _guard_coverage(st.props)
        guarded += g
        total += t
    _COVERAGE["guarded"] = guarded
    _COVERAGE["total"] = total

    return write_guard.violations(), errors, driven


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="严格模式：有裸写就返回非 0（门禁用）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out",
                    help="把结果 JSON 写到这个文件（门禁用：stdout 会混着业务日志）")
    ap.add_argument("--fast", action="store_true",
                    help="跳过全 RPC 通用形状，只跑 SCENARIOS + 街区流程（调试用）")
    args = ap.parse_args()

    violations, errors, driven = run(strict=args.strict, fast=args.fast)

    # 严格模式下没有 violations（守卫直接抛），改成从异常里取位置没有堆栈，
    # 所以门禁只看「有没有抛」；report 模式才给文件:行。
    grouped = collections.defaultdict(list)
    for fn, ln, key in violations:
        grouped[_rel(fn)].append((ln, key))

    bad = bool(errors) if args.strict else bool(violations)

    if args.json_out:
        # ★ 门禁走这条路：stdout 里混着业务日志（[sync] / [mail] …），
        #   从 stdout 抠 JSON 会解析失败（实测 Extra data），所以落文件。
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({
                "mode": "strict" if args.strict else "report",
                "driven": driven,
                "coverage": dict(_COVERAGE),
                "violations": [{"file": fn, "line": ln, "key": k}
                               for fn, ln, k in violations],
                "raised": [{"rpc": n, "error": e} for n, e in errors],
            }, f, ensure_ascii=False)
        return 1 if bad else 0

    if args.json:
        print(json.dumps({
            "mode": "strict" if args.strict else "report",
            "driven": driven,
            "coverage": dict(_COVERAGE),
            "violations": [{"file": f, "line": ln, "key": k} for f, ln, k in violations],
            "raised": [{"rpc": n, "error": e} for n, e in errors],
        }, ensure_ascii=False, indent=1))
        return 1 if bad else 0
    print("=" * 64)
    print("INV-PROPS-2 · 属性写入口（业务路径不许绕过 Tx）")
    print("=" * 64)
    print(f"驱动次数      : {driven}")
    g, t = _COVERAGE.get("guarded", 0), _COVERAGE.get("total", 0)
    if t:
        print(f"受控容器覆盖率: {g}/{t}（{g * 100 // t}%）"
              f"  ← 剩下的普通容器是守卫管不到的子树")
    if args.strict:
        print(f"严格模式抛出  : {len(errors)}")
        seen = collections.Counter(n for n, _ in errors)
        for name, n in seen.most_common(30):
            print(f"  x{n:<3} {name}")
    else:
        print(f"裸写点        : {len(violations)}")
        for f in sorted(grouped):
            print(f"  --- {f}")
            for ln, key in sorted(grouped[f]):
                print(f"      {ln:5d}  key={key}")
    print()

    if args.strict:
        if bad:
            print("有业务路径仍在裸写属性（会被守卫拒绝并使连接中断）。")
            return 1
        print("业务路径全部走 Tx / write_guard，没有裸写。")
        return 0

    if bad:
        print("上面每个位置都要改成 Tx（先构造新值再 tx.set），")
        print("或在不产生同步帧的内部簿记处加 with write_guard(理由)。")
        return 1
    print("没有发现裸写点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
