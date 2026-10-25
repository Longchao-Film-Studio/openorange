#!/usr/bin/env python3
"""修复存档里重复的 __id__（P0-A 审计 P0-3）。

## 背景

2026-10-21 审计发现：真实存档里已经存在撞号样本，例如
work/player_state/vf1.json 里 1369 同时属于
  .item_bag.1 和 .done_story_tasks.1000

客户端 customtype.lua:231-245 的 _attachToCache 会打印 id repeated!
并 cache[id] = self 覆盖缓存项，之后所有以该 id 为 propertyId 的同步
都会改到另一个对象上。

P0-A 只保证「以后不再新增撞号」，对已存在的重复既不修也不报。
本脚本负责修。

## 做法

1. 扫描 props，找出所有重复的 __id__
2. 每一组里保留**第一个**（按路径字典序），其余重新分配
3. 抬正 _oo_next_id
4. 写回存档（先备份 .bak）

## 用法

    python tools/fix_dup_ids.py --uid vf1 --dry-run
    python tools/fix_dup_ids.py --uid vf1
    python tools/fix_dup_ids.py --all --dry-run
"""
import argparse
import json
import os
import shutil
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server.property import ids as _ids  # noqa: E402


def collect(obj, path="", out=None):
    """收集所有 (path, 对象引用, id)。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        v = obj.get("__id__")
        if isinstance(v, int) and not isinstance(v, bool):
            out.append((path, obj, v))
        for k, v2 in obj.items():
            if k == "__id__":
                continue
            collect(v2, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v2 in enumerate(obj):
            collect(v2, f'{path}[{i}]', out)
    return out


def fix(props, dry_run=False):
    """修一个 props。返回 (修了几个, 报告行)。"""
    nodes = collect(props)
    by_id = {}
    for path, ref, nid in nodes:
        by_id.setdefault(nid, []).append((path, ref))

    dups = {k: v for k, v in by_id.items() if len(v) > 1}
    if not dups:
        return 0, ["没有重复的 __id__"]

    report = []
    fixed = 0
    # 先抬正计数器，保证新 id 不和现有任何 id 撞
    real_max = _ids.max_id(props)
    if not isinstance(props.get("_oo_next_id"), int) or props["_oo_next_id"] < real_max:
        props["_oo_next_id"] = real_max
        report.append(f'  校正 _oo_next_id -> {real_max}')

    for nid in sorted(dups):
        group = sorted(dups[nid], key=lambda x: x[0])
        keep = group[0]
        report.append(f'  __id__ {nid} 出现 {len(group)} 次，保留 {keep[0]}')
        for path, ref in group[1:]:
            if dry_run:
                report.append(f"    [dry-run] {path} 需要重新分配")
                continue
            new_id = _ids.alloc_id(props)
            ref["__id__"] = new_id
            fixed += 1
            report.append(f'    {path} -> {new_id}')
    return fixed, report


def main():
    ap = argparse.ArgumentParser(description="修复存档里重复的 __id__")
    ap.add_argument("--uid", default=None, help="存档 uid（work/player_state/<uid>.json）")
    ap.add_argument("--all", action="store_true", help="处理全部存档")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    args = ap.parse_args()

    from game_server import config as _cfg
    state_dir = _cfg.STATE_DIR if hasattr(_cfg, "STATE_DIR") else None
    if state_dir is None:
        state_dir = os.path.join(_ROOT, "work", "player_state")

    if args.all:
        targets = [f[:-5] for f in sorted(os.listdir(state_dir))
                   if f.endswith(".json")]
    elif args.uid:
        targets = [args.uid]
    else:
        ap.error("要么 --uid，要么 --all")

    total = 0
    for uid in targets:
        p = os.path.join(state_dir, f"{uid}.json")
        if not os.path.exists(p):
            print(f"跳过 {uid}：文件不存在")
            continue
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        props = d.get("props") or {}
        fixed, report = fix(props, dry_run=args.dry_run)
        print(f"=== {uid} ===")
        for line in report:
            print(line)
        if fixed and not args.dry_run:
            bak = p + ".bak"
            shutil.copy2(p, bak)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            print(f'  已修 {fixed} 个，备份 {bak}')
        total += fixed

    print()
    print('合计修复 {} 个{}'.format(total, "（dry-run，未写盘）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
