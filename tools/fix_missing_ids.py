#!/usr/bin/env python3
"""给存档里缺 `__id__` 的自定义类型节点补 id（按客户端声明树）。

## 什么时候用

服务端日志出现（ids.reconcile 每读档一次就会扫）：
    [ids] ⚠ 存档里有 N 处自定义类型节点缺 __id__（客户端登录会崩）

或客户端日志出现：
    [GLog] exception got: ClientAvatar:init, "CustomDict.lua:XXX: assertion failed!"

## 用法

    python tools/fix_missing_ids.py --uid 123tst            # 修（自动备份）
    python tools/fix_missing_ids.py --uid 123tst --dry-run  # 只看要补哪些
    python tools/fix_missing_ids.py --all                   # 扫全部存档

修复前会在同目录留 <uid>.json.bak-<时间戳>，改完再校验一遍。
"""
import argparse
import json
import os
import shutil
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from game_server.property import ids as _ids  # noqa: E402
from game_server.property import validate as _validate  # noqa: E402


def _save_dir():
    from game_server import config
    return getattr(config, "PLAYER_STATE_DIR", os.path.join(ROOT, "work", "player_state"))


def _fix_node(props, value, type_name, path, fixed):
    """递归补 id：能补的补，补不了的（结构坏了）只记录。"""
    if type_name is None:
        return
    from game_server.property import spec
    if spec.is_simple(type_name):
        return
    c = spec.custom(type_name)
    if c is None or not isinstance(value, dict):
        return
    if not isinstance(value.get("__id__"), int):
        nid = _ids.alloc_id(props)
        value["__id__"] = nid
        fixed.append((validate_fmt(path), type_name, nid))
    fields = (c.get("props") or {})
    for fname, fdecl in fields.items():
        if fname in value:
            ftype = fdecl[0] if isinstance(fdecl, (list, tuple)) else fdecl
            _fix_node(props, value[fname], ftype, path + (fname,), fixed)
    vtype = c.get("value_type")
    if vtype:
        for k, v in list(value.items()):
            if k in _validate.META_KEYS or k in fields or str(k).startswith("_oo_"):
                continue
            _fix_node(props, v, vtype, path + (k,), fixed)


def validate_fmt(path):
    from game_server.property.validate import _fmt
    return _fmt(path)


def fix_one(uid, dry_run=False):
    path = os.path.join(_save_dir(), f"{uid}.json")
    if not os.path.exists(path):
        print(f"  ✗ 找不到存档 {path}")
        return 1
    data = json.loads(open(path, encoding="utf-8").read())
    props = data.get("props") or {}
    missing = _validate.find_missing_ids(props)
    if not missing:
        print(f"  ✔ {uid}：没有缺 __id__ 的节点")
        return 0
    print(f"  {uid}：发现 {len(missing)} 处缺 __id__")
    for p, t in missing[:20]:
        print(f"    - {p} -> {t}")
    if len(missing) > 20:
        print(f"    …… 还有 {len(missing) - 20} 处")
    if dry_run:
        print("  （--dry-run：没有写盘）")
        return 0

    fixed = []
    from game_server.property import spec
    for name, value in props.items():
        if name in _validate.META_KEYS or str(name).startswith("_oo_"):
            continue
        sp = spec.get(name)
        if sp is None:
            continue
        _fix_node(props, value, sp.type, (name,), fixed)

    bak = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(path, bak)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    rest = _validate.find_missing_ids(props)
    print(f"  ✔ 补了 {len(fixed)} 个 id；备份 {os.path.basename(bak)}")
    for p, _t, nid in fixed[:20]:
        print(f"    + {p} -> __id__={nid}")
    if rest:
        print(f"  ⚠ 仍有 {len(rest)} 处补不了（结构坏得比较彻底）：{rest[:5]}")
        return 2
    print("  ✔ 复检：整棵树已无不变量违规")
    return 0


def main():
    ap = argparse.ArgumentParser(description="给存档补缺失的 __id__")
    ap.add_argument("--uid", help="账号名（work/player_state/<uid>.json）")
    ap.add_argument("--all", action="store_true", help="扫全部存档")
    ap.add_argument("--dry-run", action="store_true", help="只看不改")
    args = ap.parse_args()

    d = _save_dir()
    rc = 0
    if args.all:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                rc |= fix_one(fn[:-5], args.dry_run)
    elif args.uid:
        rc = fix_one(args.uid, args.dry_run)
    else:
        ap.print_help()
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
