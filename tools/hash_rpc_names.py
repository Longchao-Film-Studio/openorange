#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 genRpcIndex 当哈希预言机，把抓包里的 RPC 索引反查回名字。

背景
----
客户端 serverRpc / serverMsg 发出去的是 `phonestcore.genRpcIndex(name)` 的
32 位哈希，线上只看得到一个整数。genRpcIndex 是纯函数，所以可以反过来：
把语料里所有可能的 RPC 名字都哈希一遍，做成 名字 -> 索引 的表，再拿索引查。

用法
----
    # 1) 生成映射表（默认读 archive/game_data/LuaFull/src，可用 OO_LUA_SRC 覆盖）
    python tools/hash_rpc_names.py build

    # 2) 反查索引
    python tools/hash_rpc_names.py lookup 1033592883 1795313897

输出表默认落在 `<repo>/work/rpc_hashes.tsv`（tab 分隔：name\\thash）。

注意
----
- 只抽字符串字面量会漏掉"方法名"形式的名字（比如
  `function AvatarNetComp:networkCheckAfterChange_SC(index)` 里的那个名字
  不是字面量）。所以本工具同时把 `function <Class>:<name>` 和
  `function <Class>.<name>` 的方法名也收进候选集。
- 需要能加载 `xlua.dll`（和游戏同目录），Windows 上要先 add_dll_directory。
"""
import ctypes
import os
import re
import sys
from ctypes import POINTER, c_char_p, c_int, c_size_t, c_void_p

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_LUA_SRC = os.path.join(ROOT, "..", "archive", "game_data", "LuaFull", "src")
DEFAULT_DLL = os.path.join(
    ROOT, "..", "GuiLongchao", "game", "GuiLongchao_Data", "Plugins", "x86_64", "xlua.dll")
OUT_TSV = os.path.join(ROOT, "work", "rpc_hashes.tsv")

LITERAL_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_.:]{1,80})["']""")
METHOD_RE = re.compile(r"function\s+[A-Za-z_][\w.]*[.:]([A-Za-z_]\w{1,80})\s*\(")


def collect_names(lua_src):
    """扫描 Lua 源码，返回候选名字集合。"""
    names = set()
    n_files = 0
    for dirpath, _dirnames, filenames in os.walk(lua_src):
        for fn in filenames:
            if not fn.endswith(".lua"):
                continue
            n_files += 1
            path = os.path.join(dirpath, fn)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            names.update(m.group(1) for m in LITERAL_RE.finditer(text))
            names.update(m.group(1) for m in METHOD_RE.finditer(text))
    return sorted(names), n_files


def _open_lua(dll_path):
    """起一个裸 Lua 状态机，把 phonestcore 装成全局表。"""
    os.add_dll_directory(os.path.dirname(dll_path))
    lib = ctypes.CDLL(dll_path)
    lib.luaL_newstate.restype = c_void_p
    for fn, argtypes in (
            ("luaL_openlibs", [c_void_p]),
            ("luaopen_phonestcore", [c_void_p]),
            ("lua_setglobal", [c_void_p, c_char_p]),
            ("luaL_loadstring", [c_void_p, c_char_p]),
            ("lua_pcall", [c_void_p, c_int, c_int, c_int])):
        getattr(lib, fn).argtypes = argtypes
    lib.luaL_loadstring.restype = c_int
    lib.lua_pcall.restype = c_int
    lib.luaopen_phonestcore.restype = c_int

    state = lib.luaL_newstate()
    lib.luaL_openlibs(state)
    # phonestcore 初始化时会调一批"游戏侧回调"，缺了就直接 panic。
    # 给 _G 装个兜底 __index，任何未定义全局都返回空函数。
    stub = (b"local s=function(...) return nil end "
            b"setmetatable(_G,{__index=function(t,k) rawset(t,k,s) return s end})")
    lib.luaL_loadstring(state, stub)
    lib.lua_pcall(state, 0, 0, 0)
    lib.luaopen_phonestcore(state)
    lib.lua_setglobal(state, b"phonestcore")
    return lib, state


def cmd_build(argv):
    lua_src = os.environ.get("OO_LUA_SRC") or DEFAULT_LUA_SRC
    dll = os.environ.get("OO_XLUA_DLL") or DEFAULT_DLL
    if not os.path.isdir(lua_src):
        raise SystemExit("找不到 Lua 语料目录: %s" % lua_src)
    if not os.path.isfile(dll):
        raise SystemExit("找不到 xlua.dll: %s" % dll)

    names, n_files = collect_names(lua_src)
    print("扫描 %d 个 Lua 文件，候选名字 %d 个" % (n_files, len(names)))

    in_txt = os.path.join(ROOT, "work", "rpc_candidates.txt")
    os.makedirs(os.path.dirname(in_txt), exist_ok=True)
    with open(in_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(names))

    lib, state = _open_lua(dll)
    lua_code = """
local f = io.open('%s', 'r')
local o = io.open('%s', 'w')
for line in f:lines() do
  if #line > 0 then
    local ok, v = pcall(phonestcore.genRpcIndex, line)
    if ok then o:write(line, '\\t', tostring(v), '\\n') end
  end
end
f:close()
o:close()
""" % (in_txt.replace("\\", "/"), OUT_TSV.replace("\\", "/"))
    if lib.luaL_loadstring(state, lua_code.encode("utf-8")) != 0:
        raise SystemExit("Lua 片段加载失败")
    rc = lib.lua_pcall(state, 0, 0, 0)
    if rc != 0:
        raise SystemExit("genRpcIndex 批处理失败 rc=%s" % rc)
    n = sum(1 for _ in open(OUT_TSV, encoding="utf-8"))
    print("已写出 %s（%d 行）" % (OUT_TSV, n))


def cmd_lookup(argv):
    if not argv:
        raise SystemExit("用法: hash_rpc_names.py lookup <index> [<index> ...]")
    if not os.path.isfile(OUT_TSV):
        raise SystemExit("映射表不存在，先跑 build: %s" % OUT_TSV)
    by_hash = {}
    for line in open(OUT_TSV, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 2:
            by_hash.setdefault(parts[1], []).append(parts[0])
    for raw in argv:
        idx = str(int(raw, 0))
        names = by_hash.get(idx)
        print("%-12s -> %s" % (idx, ", ".join(names) if names else "（表里没有）"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "build":
        cmd_build(sys.argv[2:])
    elif cmd == "lookup":
        cmd_lookup(sys.argv[2:])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
