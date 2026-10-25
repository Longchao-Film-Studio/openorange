#!/usr/bin/env python3
"""tools/feature_coverage.py —— 83 个系统入口的「真实可用率」。

为什么必须有这个工具
--------------------
2026-10-22 检讨：我们连续多轮报「45 套件全绿」，但 73% 的系统入口其实打不开 ——
因为测试只覆盖了「任务能完成」，没覆盖「功能能出现」。
这个脚本把口径固定下来，数字现场生成，不许手写。

三列（对应客户端 checkSystemUnlockState 的返回）：
  数据条件  AllSystemCatalogueData.unlockType/unlockParam（客户端本地算）
  已推送    system_p.unlocked_system[sysId] == true（只能服务端推）
  按钮可见  数据条件 AND 已推送

用法
----
    python tools/feature_coverage.py                 # 默认看 ootest001
    python tools/feature_coverage.py --uid xxx
    python tools/feature_coverage.py --json

依据：docs/功能解锁research.md、docs/检讨-功能解锁遗漏.md
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("OO_STATE_DIR",
                    os.path.join(ROOT, "work", "player_state"))

from game_server import gamedata  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story.rpc import _unlock_data_satisfied  # noqa: E402

TYPE_NAME = {1: "Task 通用任务", 2: "StoryTask 剧情任务", 3: "Level 等级"}


def collect(uid):
    st = pstate.get_state(uid=uid)
    us = ((st.props.get("system_p") or {}).get("unlocked_system") or {})
    rows = []
    for row in gamedata.all_system_catalogue():
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        if not isinstance(sid, int):
            continue
        data_ok = bool(_unlock_data_satisfied(st, row))
        pushed = us.get(sid) is True
        rows.append({
            "id": sid,
            "panel": row.get("panelName") or "",
            "sysName": row.get("sysName") or "",
            "unlockType": row.get("unlockType"),
            "unlockParam": row.get("unlockParam"),
            "data_ok": data_ok,
            "pushed": pushed,
            "visible": data_ok and pushed,
        })
    return st, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="ootest001")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    st, rows = collect(args.uid)
    total = len(rows)
    n_data = sum(1 for r in rows if r["data_ok"])
    n_push = sum(1 for r in rows if r["pushed"])
    n_vis = sum(1 for r in rows if r["visible"])

    # 关键断言：数据条件满足 <=> 已推送（双向）
    miss = [r["id"] for r in rows if r["data_ok"] and not r["pushed"]]
    extra = [r["id"] for r in rows if r["pushed"] and not r["data_ok"]]

    by_type = {}
    for r in rows:
        t = r["unlockType"]
        d = by_type.setdefault(t, {"total": 0, "data": 0, "pushed": 0, "visible": 0})
        d["total"] += 1
        d["data"] += 1 if r["data_ok"] else 0
        d["pushed"] += 1 if r["pushed"] else 0
        d["visible"] += 1 if r["visible"] else 0

    report = {
        "uid": args.uid,
        "level": st.props.get("level"),
        "total": total,
        "data_satisfied": n_data,
        "pushed": n_push,
        "visible": n_vis,
        "visible_pct": round(100.0 * n_vis / max(1, total), 1),
        "by_type": {str(k): v for k, v in sorted(by_type.items())},
        "missing_push": miss,       # 数据满足但没推（= 漏解锁）
        "extra_push": extra,        # 推了但数据不满足（= 误解锁）
        "rows": rows,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if not miss and not extra else 1

    W = 72
    print("=" * W)
    print("系统入口真实可用率（tools/feature_coverage.py，现场生成）")
    print("=" * W)
    print("账号            : {}（level={}）".format(args.uid, st.props.get("level")))
    print(f'系统入口总数    : {total}')
    print(f'数据条件满足    : {n_data}')
    print(f'服务端已推送    : {n_push}')
    print('★ 按钮真实可见  : {:d}（{:.1f}%）'.format(n_vis, report["visible_pct"]))
    print("-" * W)
    print('{:<22} {:6} {:6} {:6} {:6}'.format("unlockType", "总数", "数据", "推送", "可见"))
    for k, v in sorted(by_type.items()):
        print('{:<22} {:6d} {:6d} {:6d} {:6d}'.format(TYPE_NAME.get(k, str(k)), v["total"], v["data"], v["pushed"], v["visible"]))
    print("-" * W)
    if miss:
        print(f'★ 漏解锁（数据满足但没推）{len(miss)} 个：{miss[:args.limit]}')
    else:
        print("漏解锁：无")
    if extra:
        print(f'★ 误解锁（推了但数据不满足）{len(extra)} 个：{extra[:args.limit]}')
    else:
        print("误解锁：无")
    print("=" * W)
    return 0 if not miss and not extra else 1


if __name__ == "__main__":
    sys.exit(main())