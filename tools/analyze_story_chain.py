#!/usr/bin/env python3
"""剧情与解锁链分析工具。

数据来源
--------
客户端的 BDD 数据表（archive/game_data/BDD/TableData.CommonData.*.json）。
这些表**不在仓库里**（65 MB，来自游戏安装目录），所以路径要显式给：

    python tools/analyze_story_chain.py --bdd F:/haowancheng/archive/game_data/BDD unlock

不传 --bdd 时按默认相对路径猜：<repo>/../archive/game_data/BDD

子命令
------
    scan            扫描所有表里的链式字段（前置/解锁/触发/后续）
    unlock          列出系统解锁规则（taskId -> 系统列表）
    walk <taskId>   从某个剧情任务开始，沿 PostTasks 走一遍
    actor           统计街景 NPC 的 preTask 条件分布

结论摘要见 docs/剧情与解锁链.md。
"""
import argparse
import collections
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_BDD = os.path.normpath(os.path.join(_HERE, "..", "..", "archive", "game_data", "BDD"))

PREFIX = "TableData.CommonData."

# 任务状态（common/globalconst.lua:77）
TASK_STATE = {1: "Idle", 2: "Init", 3: "Doing", 4: "WaitDone",
              5: "Done", 6: "Finished", -1: "Failed", -2: "Closed"}

CHAIN_CLASSES = [
    ("前置链", re.compile(r"^(pre[A-Z_]|Pre[A-Z_]|preTask|preLevel|preGroup|preId|preDlc|preAchievement)")),
    ("解锁条件", re.compile(r"^(unlock|Unlock)")),
    ("触发条件", re.compile(r"^(trigger|Trigger)")),
    ("后续链", re.compile(r"^(post|Post|next|Next)")),
]


def bdd_dir(arg):
    p = os.path.abspath(arg or _DEFAULT_BDD)
    if not os.path.isdir(p):
        sys.exit("找不到 BDD 目录: {}\n用 --bdd 指定（例如 F:/haowancheng/archive/game_data/BDD）".format(p))
    return p


def load(bdd, name):
    p = os.path.join(bdd, PREFIX + name + ".json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _fields(obj, out, depth=0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.add(k)
            _fields(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:3]:
            _fields(v, out, depth + 1)


def cmd_scan(bdd):
    byclass = collections.defaultdict(lambda: collections.defaultdict(set))
    for fn in sorted(os.listdir(bdd)):
        if not fn.startswith(PREFIX):
            continue
        try:
            with open(os.path.join(bdd, fn), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not d:
            continue
        fs = set()
        if isinstance(d, dict):
            for v in list(d.values())[:20]:
                _fields(v, fs)
        else:
            for v in d[:20]:
                _fields(v, fs)
        short = fn[len(PREFIX):-5]
        for cname, pat in CHAIN_CLASSES:
            for f in fs:
                if pat.match(f):
                    byclass[cname][short].add(f)

    for cname, _ in CHAIN_CLASSES:
        d = byclass[cname]
        print("\n" + "=" * 70)
        print('【{}】{} 个表'.format(cname, len(d)))
        for t in sorted(d):
            print('  {:<46} {}'.format(t, ", ".join(sorted(d[t])[:8])))


def cmd_unlock(bdd):
    data = load(bdd, "AllSystemCatalogueData")
    if data is None:
        sys.exit("缺 AllSystemCatalogueData")
    rows = data.values() if isinstance(data, dict) else data
    by = collections.defaultdict(list)
    for v in rows:
        ut, up = v.get("unlockType"), v.get("unlockParam")
        kind = {1: "Task", 2: "StoryTask", 3: "Level"}.get(ut)
        if kind:
            by[(kind, up)].append("{}({})".format(v.get("sysName"), v.get("id")))
    print("=== 系统解锁规则（unlockType / unlockParam -> 系统）===")
    for k in sorted(by, key=lambda x: (x[0], str(x[1]))):
        names = by[k]
        print('  {:<10} {:<8} -> {}'.format(k[0], k[1], ", ".join(names)))
    print("\n提示：unlockParam=999 的是「永远不开」的占位。")


def cmd_walk(bdd, start):
    st = load(bdd, "StoryTaskData")
    if st is None:
        sys.exit("缺 StoryTaskData")
    byid = {}
    for v in (st.values() if isinstance(st, dict) else st):
        byid[v.get("TaskID")] = v

    sysdata = load(bdd, "AllSystemCatalogueData") or []
    rows = sysdata.values() if isinstance(sysdata, dict) else sysdata
    unlock_by_task = collections.defaultdict(list)
    for v in rows:
        if v.get("unlockType") in (1, 2):
            unlock_by_task[v.get("unlockParam")].append(v.get("sysName"))

    cur, seen, i = int(start), set(), 0
    print('=== 从 TaskID={} 沿 PostTasks 走 ==='.format(cur))
    while cur in byid and cur not in seen and i < 200:
        seen.add(cur)
        i += 1
        v = byid[cur]
        mark = ""
        if unlock_by_task.get(cur):
            mark = "   <<< 解锁 " + " ".join(unlock_by_task[cur])
        print('  {:2d}. {:<8} type={:<2} {:<10} {:<24} {}{}'.format(i, cur, v.get("TaskType"), v.get("ChapterId") or "-", v.get("TaskName") or "", v.get("Location") or "", mark))
        posts = v.get("PostTasks") or []
        cur = posts[0] if posts else None
    if i == 0:
        print("  任务 {} 不在 StoryTaskData 里".format(start))


def cmd_actor(bdd):
    data = load(bdd, "StreetActorInfoData")
    if data is None:
        sys.exit("缺 StreetActorInfoData")
    rows = data.values() if isinstance(data, dict) else data
    states = collections.Counter()
    with_pre = 0
    for v in rows:
        pre = v.get("preTask")
        if not pre:
            continue
        with_pre += 1
        for k, s in pre.items():
            # BDD 的 OrderedDict 会混进 __bdd_metatype__ / KeyList 这类元数据键，
            # 而且 JSON 里的键都是字符串，要先转成整数再判断。
            if isinstance(k, str) and k.lstrip('-').isdigit():
                k = int(k)
            if not isinstance(k, int):
                continue
            try:
                s = int(s)
            except Exception:
                continue
            states[s] += 1
    print("=== 街景 NPC 的 preTask 条件分布 ===")
    print('StreetActorInfoData 共 {} 条，其中 {} 条带 preTask'.format(len(rows), with_pre))
    print()
    for s, n in states.most_common():
        print('  state={:<3} {:<10}  {:d} 条'.format(s, TASK_STATE.get(s, "?"), n))
    print()
    print("★ state=3(Doing) 表示「该任务进行中才出现」，")
    print("  所以把任务标成 Finished(6) 反而会让这些 NPC 消失。")


def main():
    ap = argparse.ArgumentParser(description="剧情与解锁链分析")
    ap.add_argument("--bdd", default=None, help="BDD 目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("unlock")
    p_walk = sub.add_parser("walk")
    p_walk.add_argument("task_id")
    sub.add_parser("actor")
    args = ap.parse_args()

    bdd = bdd_dir(args.bdd)
    if args.cmd == "scan":
        cmd_scan(bdd)
    elif args.cmd == "unlock":
        cmd_unlock(bdd)
    elif args.cmd == "walk":
        cmd_walk(bdd, args.task_id)
    elif args.cmd == "actor":
        cmd_actor(bdd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
