#!/usr/bin/env python3
"""把归龙潮的 BDD 配置表数据库（pmdata.bin）完整导出。

原理：
  - pmdata.bin 是 BDD（Binary Design Data）格式，由 xlua.dll 里的原生 bdd 模块加载
  - 游戏里 require("TableData.*") 被 Utils.RequireHook 劫持，优先走 bddDataMgr:getTable()
  - 所以 Lua 的 tabledata/*.luac 只是兜底，真数据全在 pmdata.bin
  - xlua.dll 导出了完整的 Lua 5.1 API 和 luaopen_bdd，所以可以独立加载

用法:
    python tools/dump_bdd.py --pmdata <pmdata.bin> --out <输出目录>
"""
import argparse
import ctypes
import os
import sys
from ctypes import POINTER, c_char_p, c_int, c_size_t, c_void_p

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LUA_SCRIPT = r'''
local root = bdd.get_ud_by_addr(bdd.get_start_handle())
local raw = bdd.ger_raw_table(root)
if type(raw) ~= 'table' then
  io.write('ERR: root not table\n'); return
end

local OUT = ...
local ok, err = pcall(function()
  local f = io.open(OUT .. '/_index.txt', 'w')
  local n = 0
  for k, _ in pairs(raw) do
    f:write(k, '\n')
    n = n + 1
  end
  f:close()
  io.write('表总数=', n, '\n')
end)
if not ok then io.write('index err: ', tostring(err), '\n') end

-- 序列化成 JSON
local function esc(s)
  s = string.gsub(s, '\\', '\\\\')
  s = string.gsub(s, '"', '\\"')
  s = string.gsub(s, '\n', '\\n')
  s = string.gsub(s, '\r', '\\r')
  s = string.gsub(s, '\t', '\\t')
  -- 其余控制字符转 \uXXXX
  s = string.gsub(s, '.', function(c)
    local b = string.byte(c)
    if b and b < 32 then return string.format('\\u%04x', b) end
    return c
  end)
  return s
end

local function is_array(t)
  if t[1] == nil then return false end
  local n = 0
  for _ in pairs(t) do n = n + 1 end
  return n == #t
end

local function ser(v, depth)
  local t = type(v)
  if t == 'nil' then return 'null'
  elseif t == 'string' then return '"' .. esc(v) .. '"'
  elseif t == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    return string.format('%.17g', v)
  elseif t == 'boolean' then return tostring(v)
  elseif t == 'table' then
    if depth > 24 then return '"<too-deep>"' end
    local parts = {}
    if is_array(v) then
      for i = 1, #v do parts[#parts+1] = ser(v[i], depth + 1) end
      return '[' .. table.concat(parts, ',') .. ']'
    else
      for kk, vv in pairs(v) do
        local key
        if type(kk) == 'number' then key = '"' .. string.format('%d', kk) .. '"'
        elseif type(kk) == 'string' then key = '"' .. esc(kk) .. '"'
        else key = '"' .. esc(tostring(kk)) .. '"' end
        parts[#parts+1] = key .. ':' .. ser(vv, depth + 1)
      end
      return '{' .. table.concat(parts, ',') .. '}'
    end
  else
    return '"<' .. t .. '>"'
  end
end

local ok2, err2 = pcall(function()
  local done = 0
  for name, tbl in pairs(raw) do
    done = done + 1
    local safe = string.gsub(name, '[\\/:*?"<>|]', '_')
    local path = OUT .. '/' .. safe .. '.json'
    local f = io.open(path, 'w')
    if f then
      f:write(ser(tbl, 0))
      f:close()
    end
    if done % 500 == 0 then io.write('  已写 ', done, '\n') end
  end
  io.write('完成，共写 ', done, ' 张表\n')
end)
if not ok2 then io.write('dump err: ', tostring(err2), '\n') end
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pmdata", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--xlua", required=True)
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    pm = os.path.abspath(args.pmdata).replace("\\", "/")
    out_lua = out.replace("\\", "/")

    os.add_dll_directory(os.path.dirname(os.path.abspath(args.xlua)))
    lib = ctypes.CDLL(args.xlua)
    lib.luaL_newstate.restype = c_void_p
    lib.luaL_newstate.argtypes = []
    lib.luaL_openlibs.argtypes = [c_void_p]
    lib.luaopen_bdd.restype = c_int
    lib.luaopen_bdd.argtypes = [c_void_p]
    lib.lua_setglobal.argtypes = [c_void_p, c_char_p]
    lib.luaL_loadstring.argtypes = [c_void_p, c_char_p]
    lib.luaL_loadstring.restype = c_int
    lib.lua_pcall.argtypes = [c_void_p, c_int, c_int, c_int]
    lib.lua_pcall.restype = c_int
    lib.lua_tolstring.argtypes = [c_void_p, c_int, POINTER(c_size_t)]
    lib.lua_tolstring.restype = c_char_p
    lib.lua_settop.argtypes = [c_void_p, c_int]

    L = lib.luaL_newstate()
    if not L:
        print("luaL_newstate 失败"); return 1
    lib.luaL_openlibs(L)
    lib.luaopen_bdd(L)
    lib.lua_setglobal(L, b"bdd")

    def run(script, label):
        lib.luaL_loadstring(L, script.encode("utf-8"))
        rc = lib.lua_pcall(L, 0, 0, 0)
        if rc != 0:
            n = c_size_t(0)
            msg = lib.lua_tolstring(L, -1, ctypes.byref(n))
            print("  [{}] 失败: {}".format(label, msg.decode("utf-8", "replace") if msg else "?"))
            lib.lua_settop(L, 0)
            return False
        lib.lua_settop(L, 0)
        return True

    print("初始化 bdd ...")
    run("bdd.init()", "init")
    print("加载 pmdata.bin ...")
    run("pcall(bdd.load_from_file, '{}')".format(pm), "load")

    script = LUA_SCRIPT.replace("local OUT = ...", "local OUT = '{}'".format(out_lua))
    print("开始导出到", out)
    run(script, "dump")
    return 0


if __name__ == "__main__":
    sys.exit(main())
