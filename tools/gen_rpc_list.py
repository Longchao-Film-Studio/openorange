#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成归龙潮的完整 RPC 总表。

数据来源（三处合并）
------------------
1. **ServerOnlyMsg**（generated/config.lua）
     服务端 -> 客户端的消息声明，共 147 条，带参数类型。
2. **serverRpc(...) / serverMsg(...) 调用**（全部 Lua 源码）
     客户端 -> 服务端的 RPC，534 个不重复名字，从调用点还能拿到所在文件。
3. **genRpcIndex**（xlua.dll 的真身哈希）
     名字 -> 32 位索引。work/rpc_hashes.tsv 里已经算好 94,589 个名字。

输出
----
    docs/RPC总表.md      人看：分两向、标已实现/未实现
    work/rpc_list.tsv    机读：direction\tname\tindex\timplemented\tparams\tsites

用法
----
    python tools/gen_rpc_list.py
    python tools/gen_rpc_list.py --check    # 只打印统计，不写文件
"""
import argparse
import collections
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
LUA_SRC = os.environ.get("OO_LUA_SRC") or r"F:\haowancheng\archive\game_data\LuaFull\src"
CONFIG_LUA = os.path.join(LUA_SRC, "generated", "config.lua")
RPC_HASHES = os.path.join(_ROOT, "work", "rpc_hashes.tsv")
OUT_MD = os.path.join(_ROOT, "docs", "RPC总表.md")
OUT_TSV = os.path.join(_ROOT, "work", "rpc_list.tsv")


def parse_server_only_msg():
    """从 config.lua 抽出 ServerOnlyMsg 声明：name -> 参数类型串。"""
    out = collections.OrderedDict()
    with open(CONFIG_LUA, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    i = 0
    while i < len(lines):
        if "ServerOnlyMsg = {" not in lines[i]:
            i += 1
            continue
        depth = 0
        j = i
        while j < len(lines):
            depth += lines[j].count("{") - lines[j].count("}")
            s = lines[j].strip()
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{(.*)", s)
            if m and "ServerOnlyMsg" not in s:
                name = m.group(1)
                rest = m.group(2).strip()
                if rest.endswith("},"):
                    rest = rest[:-2]
                elif rest.endswith("}"):
                    rest = rest[:-1]
                out[name] = rest.strip()
            if depth <= 0 and j > i:
                break
            j += 1
        i = j + 1
    return out


def scan_client_calls():
    """扫所有 Lua，收集 serverRpc/serverMsg 的名字和调用点。"""
    sites = collections.defaultdict(list)
    pat = re.compile(r"server(Rpc|Msg)\(\s*\"([A-Za-z0-9_]+)\"")
    for dp, dn, fn in os.walk(LUA_SRC):
        for f in fn:
            if not f.endswith(".lua"):
                continue
            p = os.path.join(dp, f)
            try:
                t = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            for i, ln in enumerate(t.split("\n"), 1):
                for m in pat.finditer(ln):
                    name = m.group(2)
                    sites[name].append((os.path.relpath(p, LUA_SRC), i))
    return sites


def load_hashes():
    """读 work/rpc_hashes.tsv：name -> index。"""
    out = {}
    if not os.path.exists(RPC_HASHES):
        return out
    with open(RPC_HASHES, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 2:
                out[p[0]] = p[1]
    return out


def load_implemented():
    """我们服务端 entity_rpc.RPC_INDEX 里已经实现的 RPC 名。"""
    sys.path.insert(0, _ROOT)
    try:
        from game_server import entity_rpc
        return set(entity_rpc.RPC_INDEX.keys())
    except Exception as e:
        print("[warn] 读不到 entity_rpc.RPC_INDEX: %s" % e)
        return set()


def main():
    ap = argparse.ArgumentParser(description="生成完整 RPC 总表")
    ap.add_argument("--check", action="store_true", help="只打印统计")
    args = ap.parse_args()

    if not os.path.isdir(LUA_SRC):
        raise SystemExit("找不到 Lua 源码目录: %s（用 OO_LUA_SRC 指定）" % LUA_SRC)

    sonly = parse_server_only_msg()
    sites = scan_client_calls()
    hashes = load_hashes()
    implemented = load_implemented()

    print("服务端->客户端（ServerOnlyMsg）: %d 条" % len(sonly))
    print("客户端->服务端（serverRpc/serverMsg）: %d 条" % len(sites))
    print("genRpcIndex 已算好: %d 个名字" % len(hashes))
    print("我们已实现: %d 条" % len(implemented))
    miss = [n for n in list(sonly) + list(sites) if n not in hashes]
    print("还没算索引的: %d 条" % len(miss))

    if args.check:
        return 0

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    os.makedirs(os.path.dirname(OUT_TSV), exist_ok=True)

    # TSV
    with open(OUT_TSV, "w", encoding="utf-8") as f:
        f.write("direction\tname\tindex\timplemented\tparams\tsites\n")
        for name, params in sonly.items():
            f.write("S2C\t%s\t%s\t%s\t%s\t\n" % (
                name, hashes.get(name, ""),
                "yes" if name in implemented else "no", params))
        for name, ss in sorted(sites.items()):
            f.write("C2S\t%s\t%s\t%s\t\t%s\n" % (
                name, hashes.get(name, ""),
                "yes" if name in implemented else "no",
                ";".join("%s:%d" % s for s in ss[:3])))

    # Markdown
    lines = []
    lines.append("# 归橙 · RPC 总表")
    lines.append("")
    lines.append("> 由 `tools/gen_rpc_list.py` 生成，**不要手改**。")
    lines.append(">")
    lines.append("> 索引由 `xlua.dll` 的真身 `genRpcIndex(name)` 算出，不是猜的。")
    lines.append("")
    lines.append("## 来源与可信度（2026-10-20 追查结论）")
    lines.append("")
    lines.append("客户端的 RPC 分五类，定义在 `core/common/const.lua:8-14`：")
    lines.append("")
    lines.append("```lua")
    lines.append("ACCESSOR_INT_TO_STRING = {")
    lines.append('  "ClientOnlyMsg",   -- 1 客户端 -> 服务端')
    lines.append('  "ServerOnlyMsg",   -- 2 服务端 -> 客户端')
    lines.append('  "ChannelMsg",      -- 3')
    lines.append('  "EngineMsg",       -- 4')
    lines.append('  "GmMethod"         -- 5')
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("`core/common/entityfactory.lua:160-180` 的 `updateRpcArg` 会遍历")
    lines.append("`Config.Entities` / `Components` / `Universal` 的这五段，把每个方法名")
    lines.append("交给 `EntityFactory:registerRpc(name)` -> `phonestcore.registerRpc(name)`。")
    lines.append("")
    lines.append("**但客户端 config.lua 实际只填了两段**：")
    lines.append("")
    lines.append("| 段 | config.lua 里的条数 |")
    lines.append("|---|---|")
    lines.append("| `ClientOnlyMsg` | **0（不存在）** |")
    lines.append("| `ServerOnlyMsg` | 147 |")
    lines.append("| `ChannelMsg` | **0（不存在）** |")
    lines.append("| `EngineMsg` | 2 |")
    lines.append("| `GmMethod` | **0（不存在）** |")
    lines.append("")
    lines.append("所以：")
    lines.append("")
    lines.append("- **服务端 → 客户端**：有权威声明（`ServerOnlyMsg` + `EngineMsg`），可信。")
    lines.append("- **客户端 → 服务端**：**没有任何声明**。名字只以字符串字面量出现在")
    lines.append("  `serverRpc(\"名字\", ...)` 的调用点，发给服务端时由原生层 `genRpcIndex(name)`")
    lines.append("  现算 32 位索引。所以只能靠扫调用点枚举 —— 这是唯一来源。")
    lines.append("")
    lines.append("运行时 `phonestcore.registerRpc` 只登记**需要反查名字的**那些（即 S2C），")
    lines.append("加上实体类名；实测一次运行只登记 240 个名字，远少于静态扫描的 534 条 C2S。")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 方向 | 条数 | 我们已实现 |")
    lines.append("|---|---|---|")
    c2s_impl = len([n for n in sites if n in implemented])
    s2c_impl = len([n for n in sonly if n in implemented])
    lines.append("| 客户端 → 服务端 | %d | %d |" % (len(sites), c2s_impl))
    lines.append("| 服务端 → 客户端 | %d | %d |" % (len(sonly), s2c_impl))
    lines.append("")
    lines.append("## 服务端 → 客户端（ServerOnlyMsg 声明，带参数类型）")
    lines.append("")
    lines.append("| 消息 | 索引 | 已实现 | 参数 |")
    lines.append("|---|---|---|---|")
    for name, params in sonly.items():
        lines.append("| `%s` | %s | %s | %s |" % (
            name, hashes.get(name, "?"),
            "✅" if name in implemented else "",
            params.replace("|", "\\|") or "(无)"))
    lines.append("")
    lines.append("## 客户端 → 服务端（serverRpc / serverMsg 调用点）")
    lines.append("")
    lines.append("| RPC | 索引 | 已实现 | 调用点（最多 3 处） |")
    lines.append("|---|---|---|---|")
    for name, ss in sorted(sites.items()):
        lines.append("| `%s` | %s | %s | %s |" % (
            name, hashes.get(name, "?"),
            "✅" if name in implemented else "",
            "<br>".join("%s:%d" % s for s in ss[:3])))
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n已写出:")
    print("  %s" % OUT_MD)
    print("  %s" % OUT_TSV)
    return 0


if __name__ == "__main__":
    sys.exit(main())