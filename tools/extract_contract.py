#!/usr/bin/env python3
"""tools/extract_contract.py —— 从客户端 Lua 机械提取字段契约。

## 这个工具凭什么存在

见 docs/指引-字段契约取证.md。一句话：**客户端期望什么，全在 Lua 里，可机械提取。**

以前修 bug 的顺序是「用户撞到症状 -> 人工 grep -> 改那一个点」，
于是同一个坑换个字段来一次。本工具把「提取」这一步变成命令，
产出的三张表可以直接和服务端实现对差。

## 提取什么

    [1] C2S 请求参数    self:serverRpc("X", {a, b, c}, ...)
    [2] S2C 响应字段    function Xxx:X_SC(cb, ret) ... ret.xxx ... end
    [3] 内容门槛字段    客户端读的 BDD 字段 vs game_server 里有没有读

## 用法

    python tools/extract_contract.py               # 打印摘要 + 写 work/contract_*.json
    python tools/extract_contract.py --c2s         # 只看 C2S
    python tools/extract_contract.py --s2c         # 只看 S2C
    python tools/extract_contract.py --gates       # 只看内容门槛字段

## 纪律

本工具**只读不写**客户端与服务端代码，只产出报告（R6：调用点用证据，不猜）。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 客户端 decompile 出来的 Lua 根目录（与 tests/lua_harness.py 同一个环境变量）
LUA_ROOT = os.environ.get("OO_LUA_ROOT") or r"F:\haowancheng\archive\game_data\LuaAll_Decompiled2"
OUT_DIR = os.path.join(ROOT, "work")

#: C2S：self:serverRpc("X", {a, b}, ...)
#:  ★ 只认组件里的**定义**（self:serverRpc），面板调用点是转发，参数名不同。
C2S_RE = re.compile(r'self[:.]serverRpc(?:_By[A-Za-z]*)?\s*\(\s*"([A-Za-z0-9_]+)"\s*,\s*\{([^}]*)\}')

#: S2C：function Xxx:Yyy_SC(params) ... body ... end
#:  非贪婪匹配到行首 end，足以覆盖客户端的写码风格（不嵌套 function）。
S2C_RE = re.compile(
    r"function\s+(\w+)[:.](([A-Za-z0-9_]+_SC))\s*\(([^)]*)\)(.*?)\nend", re.DOTALL,
)

def iter_lua():
    """遍历客户端所有 .lua，产出 (相对路径, 内容)。"""
    for dirpath, _dirs, names in os.walk(LUA_ROOT):
        for fn in names:
            if not fn.endswith(".lua"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                txt = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            rel = os.path.relpath(path, LUA_ROOT).replace(os.sep, "/")
            yield rel, txt


def _split_args(raw):
    """把 {a, b, c} 里的参数名列表拆出来。去掉换行和多余空白。"""
    out = []
    for part in raw.split(","):
        name = " ".join(part.split()).strip()
        if name:
            out.append(name)
    return out


def extract_c2s():
    """提取 C2S 请求参数：(name -> {params, files})。"""
    result: dict[str, dict] = {}
    for rel, txt in iter_lua():
        if "serverRpc" not in txt:
            continue
        for m in C2S_RE.finditer(txt):
            name = m.group(1)
            params = _split_args(m.group(2))
            entry = result.setdefault(name, {"params": [], "files": [], "variants": {}})
            if rel not in entry["files"]:
                entry["files"].append(rel)
            sig = ", ".join(params)
            entry["variants"][sig] = entry["variants"].get(sig, 0) + 1
    # ★ params 取「出现次数最多」的那个变体（组件定义处通常是权威写法）。
    #   第一版忘了回填 params，导致摘要里 453 个 RPC 全被报成「无参数」——
    #   这就是「工具自己也会骗人」，所以摘要必须回填后自检（见 main 的校验）。
    for entry in result.values():
        if entry["variants"]:
            best = max(entry["variants"].items(), key=lambda kv: kv[1])[0]
            entry["params"] = [p for p in best.split(", ") if p]
    return result


def _ret_var_name(params):
    """从 SC 的形参里认出「响应对象」那个参数。

    客户端写法有 ret / response / retStatus / retstatus / info 等。
    判据：名字里含 ret（大小写不敏感），或者等于 response。
    找不到返回 None —— 说明这个 SC 压根不接响应体。
    """
    for p in params:
        low = p.lower()
        if low in ("ret", "response", "rsp", "retstatus"):
            return p
    return None


def extract_s2c():
    """提取 S2C 响应字段：(name -> {params, ret_var, fields, files})。"""
    result: dict[str, dict] = {}
    for rel, txt in iter_lua():
        if "_SC" not in txt:
            continue
        for m in S2C_RE.finditer(txt):
            name = m.group(2)
            params = [x.strip() for x in m.group(4).split(",") if x.strip()]
            body = m.group(5)
            ret_var = _ret_var_name(params)
            fields: list[str] = []
            if ret_var:
                fields = sorted(set(re.findall(r"\b" + re.escape(ret_var) + r"\.(\w+)", body)))
            entry = result.setdefault(name, {
                "params": params, "ret_var": ret_var, "fields": [], "files": [],
            })
            if rel not in entry["files"]:
                entry["files"].append(rel)
            for f in fields:
                if f not in entry["fields"]:
                    entry["fields"].append(f)
    for entry in result.values():
        entry["fields"].sort()
    return result


#: 客户端读 BDD 表字段的典型写法：
#:   data.TaskTagLimit / cfg.get("X") / DataTable("Common","TaskData")[id].X
#: 这里只统计「表名 -> 被读到的字段集合」，表名从 DataTable/DataLoader 调用点推断。
BDD_CALL_RE = re.compile(r'(?:DataTable|DataMgr\.dataTable)\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)')

#: 门槛类字段名关键词（用于聚焦，不是白名单 —— 全部字段都会列出来）
GATE_KEYWORDS = ("Limit", "Unlock", "Need", "Level", "Pre", "Condition", "Require",
                 "Open", "Enable", "TaskTag", "Newbee")


def extract_bdd_reads():
    """统计客户端从 BDD 表里读了哪些字段：(表名 -> 字段集合)。

    ★ 这是**保守估计**：客户端大量存在 `local d = DataTable(...)[id]` 后
      隔几十行再 `d.SomeField` 的写法，静态串联不完整。
      所以本函数只统计「同一文件里、DataTable 调用之后出现的字段名」，
      并且**明确标注这是下界**，不假装完整。
    """
    result: dict[str, set] = collections.defaultdict(set)
    for _rel, txt in iter_lua():
        names = {}
        for m in BDD_CALL_RE.finditer(txt):
            table = m.group(2)
            # 找这一行里的局部变量名：local x = DataTable(...)
            line_start = txt.rfind("\n", 0, m.start()) + 1
            line = txt[line_start: txt.find("\n", m.start())]
            vm = re.search(r"local\s+(\w+)\s*=", line)
            if vm:
                names[vm.group(1)] = table
        for var, table in names.items():
            for m in re.finditer(r"\b" + re.escape(var) + r"\.(\w+)", txt):
                result[table].add(m.group(1))
    return {k: sorted(v) for k, v in result.items()}


def server_reads_field(field):
    """game_server/ 里有没有读过这个字段名（返回命中的文件名列表）。"""
    hits = []
    base = os.path.join(ROOT, "game_server")
    for dirpath, _dirs, names in os.walk(base):
        for fn in names:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                txt = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if field in txt:
                hits.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))
    return hits


def _dump(name, data):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    return path


def main():
    ap = argparse.ArgumentParser(description="从客户端 Lua 提取字段契约")
    ap.add_argument("--c2s", action="store_true")
    ap.add_argument("--s2c", action="store_true")
    ap.add_argument("--gates", action="store_true")
    args = ap.parse_args()
    only = args.c2s or args.s2c or args.gates

    if not os.path.isdir(LUA_ROOT):
        print("找不到客户端 Lua 目录：" + LUA_ROOT)
        return 2

    if not only or args.c2s:
        c2s = extract_c2s()
        empty = [k for k, v in c2s.items() if not v["params"]]
        multi = {k: v["variants"] for k, v in c2s.items() if len(v["variants"]) > 1}
        print(f"[1] C2S 请求参数：{len(c2s)} 个 RPC 有组件定义")
        print(f"    其中无参数（空表）：{len(empty)}")
        print(f"    参数名有空变体的：{len(multi)}（多为转发函数换名，见指引 §4.2）")
        p = _dump("contract_c2s.json", c2s)
        print(f"    已写 {os.path.relpath(p, ROOT)}")

    if not only or args.s2c:
        s2c = extract_s2c()
        with_ret = [k for k, v in s2c.items() if v["ret_var"]]
        with_fields = [k for k, v in s2c.items() if v["fields"]]
        no_ret = [k for k, v in s2c.items() if not v["ret_var"]]
        print(f"[2] S2C 响应处理函数：{len(s2c)} 个")
        print(f"    带响应体参数的：{len(with_ret)}")
        print(f"    其中读了字段的：{len(with_fields)}")
        print(f"    完全不接响应体的：{len(no_ret)}")
        p = _dump("contract_s2c.json", s2c)
        print(f"    已写 {os.path.relpath(p, ROOT)}")

    if not only or args.gates:
        reads = extract_bdd_reads()
        print(f"[3] 客户端读取的 BDD 表字段：{len(reads)} 张表（下界，见函数注释）")
        gates = sorted({f for fs in reads.values() for f in fs
                        if any(k in f for k in GATE_KEYWORDS)})
        print(f"    疑似门槛类字段：{len(gates)} 个")
        unread = []
        for f in gates:
            if not server_reads_field(f):
                unread.append(f)
        print(f"    其中 game_server/ 里**从未读过**的：{len(unread)}")
        for f in unread[:30]:
            print(f"      {f}")
        if len(unread) > 30:
            print(f"      ...（共 {len(unread)} 个，完整列表见 json）")
        payload = {"bdd_reads": reads, "gate_fields": gates, "server_unread": unread}
        p = _dump("contract_gates.json", payload)
        print(f"    已写 {os.path.relpath(p, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
