#!/usr/bin/env python3
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
from ctypes import c_char_p, c_int, c_void_p

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
        raise SystemExit("找不到 Lua 语料目录: {}".format(lua_src))
    if not os.path.isfile(dll):
        raise SystemExit("找不到 xlua.dll: {}".format(dll))

    names, n_files = collect_names(lua_src)
    print("扫描 %d 个 Lua 文件，候选名字 %d 个" % (n_files, len(names)))

    in_txt = os.path.join(ROOT, "work", "rpc_candidates.txt")
    os.makedirs(os.path.dirname(in_txt), exist_ok=True)
    with open(in_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(names))

    lib, state = _open_lua(dll)
    lua_code = """
local f = io.open('{}', 'r')
local o = io.open('{}', 'w')
for line in f:lines() do
  if #line > 0 then
    local ok, v = pcall(phonestcore.genRpcIndex, line)
    if ok then o:write(line, '\\t', tostring(v), '\\n') end
  end
end
f:close()
o:close()
""".format(in_txt.replace("\\", "/"), OUT_TSV.replace("\\", "/"))
    if lib.luaL_loadstring(state, lua_code.encode("utf-8")) != 0:
        raise SystemExit("Lua 片段加载失败")
    rc = lib.lua_pcall(state, 0, 0, 0)
    if rc != 0:
        raise SystemExit("genRpcIndex 批处理失败 rc={}".format(rc))
    n = sum(1 for _ in open(OUT_TSV, encoding="utf-8"))
    print("已写出 %s（%d 行）" % (OUT_TSV, n))


def cmd_lookup(argv):
    if not argv:
        raise SystemExit("用法: hash_rpc_names.py lookup <index> [<index> ...]")
    if not os.path.isfile(OUT_TSV):
        raise SystemExit("映射表不存在，先跑 build: {}".format(OUT_TSV))
    by_hash = {}
    for line in open(OUT_TSV, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 2:
            by_hash.setdefault(parts[1], []).append(parts[0])
    for raw in argv:
        idx = str(int(raw, 0))
        names = by_hash.get(idx)
        print("%-12s -> %s" % (idx, ", ".join(names) if names else "（表里没有）"))


def cmd_verify(argv):
    """用真身 genRpcIndex 重新核对 game_server/entity_rpc.py 里的索引表。

    这一步是「不靠猜」的闭环：代码里每个硬编码的索引都能被机器复算一遍。
    """
    dll = os.environ.get("OO_XLUA_DLL") or DEFAULT_DLL
    if not os.path.isfile(dll):
        raise SystemExit("找不到 xlua.dll: {}".format(dll))
    sys.path.insert(0, ROOT)
    from game_server.entity_rpc import RPC_INDEX

    lib, state = _open_lua(dll)
    lua_lines = ["local f = io.open('{}', 'w')".format(os.path.join(
        ROOT, "work", "verify_hashes.tsv").replace("\\", "/"))]
    lua_lines.append("local names = {")
    for nm in RPC_INDEX:
        lua_lines.append('  "{}",'.format(nm))
    lua_lines.append("}")
    lua_lines.append("for _, n in ipairs(names) do")
    lua_lines.append("  local ok, v = pcall(phonestcore.genRpcIndex, n)")
    lua_lines.append("  if ok and f then f:write(n, '\\t', tostring(v), '\\n') end")
    lua_lines.append("end")
    lua_lines.append("if f then f:close() end")
    code = "\n".join(lua_lines)
    if lib.luaL_loadstring(state, code.encode("utf-8")) != 0:
        raise SystemExit("Lua 片段加载失败")
    if lib.lua_pcall(state, 0, 0, 0) != 0:
        raise SystemExit("genRpcIndex 失败")

    out = os.path.join(ROOT, "work", "verify_hashes.tsv")
    bad = 0
    n = 0
    for line in open(out, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 2:
            continue
        nm, idx = parts[0], int(parts[1])
        n += 1
        if RPC_INDEX[nm] != idx:
            bad += 1
            print("  MISMATCH %-40s 代码=%s 真身=%s" % (nm, RPC_INDEX[nm], idx))
    print("核对 %d 条，不一致 %d 条" % (n, bad))
    return 0 if bad == 0 else 1


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
    elif cmd == "verify":
        return cmd_verify(sys.argv[2:])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
