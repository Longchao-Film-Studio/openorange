
"""生成 docs/RPC缺口分析.md"""
import collections
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = r"F:\haowancheng\OpenOrange"
TSV = os.path.join(ROOT, "work", "rpc_list.tsv")
OUT = os.path.join(ROOT, "docs", "RPC缺口分析.md")
BT = chr(96)

rows = []
for line in open(TSV, encoding="utf-8"):
    p = line.rstrip("\n").split("\t")
    if len(p) >= 6 and p[0] in ("C2S", "S2C"):
        rows.append({"dir": p[0], "name": p[1], "index": p[2], "impl": p[3], "params": p[4], "sites": p[5]})

c2s = [r for r in rows if r["dir"] == "C2S"]
s2c = [r for r in rows if r["dir"] == "S2C"]

def bucket(r):
    s = r["sites"].split(";")[0]
    f = s.split(":")[0].replace("\\", "/")
    base = os.path.basename(f)
    m = re.match(r"avatar(.+?)comp\.lua$", base)
    if m:
        return m.group(1)
    if base.startswith("avatar"):
        return base[6:-4] or base
    if "space" in f.lower():
        return "space"
    if "clientaccount" in base:
        return "account"
    if "clientavatar" in base:
        return "avatar"
    if "clientsoul" in base:
        return "soul"
    if "core/" in f:
        return "core"
    if "debug" in base.lower():
        return "debug"
    return base[:-4]

DEBUG = {"debug", "rankdebugcomp", "gachadebugcomp", "donatedebugcomp", "gm", "core"}

groups = collections.defaultdict(list)
for r in c2s:
    groups[bucket(r)].append(r)

debug_n = sum(len(groups[k]) for k in DEBUG if k in groups)
real_n = len(c2s) - debug_n

TIER2 = ["street", "streetactor", "space", "aoi", "account", "avatar", "soul",
         "storytask", "task", "storyline", "namecomp", "createrole", "level",
         "dungeon", "bag", "character", "formation", "userdata"]

lines = []
A = lines.append
A("# 归橙 · RPC 缺口分析")
A("")
A("> 由 tools/rpc/gen_gap_doc.py 从 work/rpc_list.tsv 生成。")
A("> 数据基础：C2S 572 条（客户端发起的全部）、S2C 147 条（服务端下发的全部）。")
A("")
A("## 一、总量")
A("")
A("| | 条数 |")
A("|---|---|")
A('| C2S（客户端 → 服务端） | {} |'.format(len(c2s)))
A('| S2C（服务端 → 客户端） | {} |'.format(len(s2c)))
A('| 我们已实现 | {} |'.format(len([r for r in rows if r["impl"] == "yes"])))
A('| **C2S 里属于调试/GM 的** | **{}** |'.format(debug_n))
A('| **真实业务 RPC** | **{}** |'.format(real_n))
A("")
A("调试/GM 类（debug 82 + rankdebug 21 + gachadebug 8 + donatedebug 3 + gm 9 + core 3）")
A("**不需要实现** —— 那是策划调试面板和 GM 命令，正常玩家走不到。")
A("")
A("## 二、阶段 2 必需的系统")
A("")
A("这几个是「能在街区正常玩」的前提，按依赖顺序排：")
A("")
A("| 系统 | 条数 | 已实现 |")
A("|---|---|---|")
for k in TIER2:
    if k not in groups:
        continue
    g = groups[k]
    done = len([r for r in g if r["impl"] == "yes"])
    A('| {} | {} | {} |'.format(k, len(g), done))
A("")
A("### 明细")
A("")
for k in TIER2:
    if k not in groups:
        continue
    g = sorted(groups[k], key=lambda r: r["name"])
    A('#### {}（{} 条）'.format(k, len(g)))
    A("")
    A("| RPC | 索引 | 已实现 | 调用点 |")
    A("|---|---|---|---|")
    for r in g:
        A("| " + BT + r["name"] + BT + " | " + r["index"] + " | " + ("是" if r["impl"] == "yes" else "") + " | " + r["sites"].split(";")[0] + " |")
    A("")

A("## 三、全部系统一览")
A("")
A("| 系统 | C2S 条数 | 已实现 |")
A("|---|---|---|")
for k in sorted(groups, key=lambda x: -len(groups[x])):
    g = groups[k]
    done = len([r for r in g if r["impl"] == "yes"])
    mark = "（调试）" if k in DEBUG else ""
    A('| {}{} | {} | {} |'.format(k, mark, len(g), done))
A("")

open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print('已写出 {}（{} 行）'.format(OUT, len(lines)))
print('C2S {}，调试类 {}，真实业务 {}'.format(len(c2s), debug_n, real_n))
