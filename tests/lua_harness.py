#!/usr/bin/env python3
"""同源 Lua 运行时（E2E 测试用）。

## 为什么

「服务端造的数据客户端能不能解析」这件事，以前只能用真客户端试。
2026-10-26 两次「整号登不上」就是这么漏过去的：服务端数据结构不合法，
客户端 CustomDict 断言失败，服务端日志完全正常。

这里用 lupa 内嵌 **Lua 5.3**（与游戏 xLua 同版本），直接加载
archive/ 里 decompile 出来的**同一份客户端 Lua**，让它去解析服务端产出的数据。
这是 E2E 而不是 mock：跑的是客户端自己的 roinit / 类型工厂代码。

## 用法

    from tests.lua_harness import new_runtime

    lua = new_runtime()
    lua.execute('local m = require("Core.PropertySync.CustomDict")')

## 兼容性

- 必须用 lua53：lupa 也带 5.5，但 5.5 的 const 推断会让 decompile 代码报
  "attempt to assign to const variable"，不是客户端的问题。
- 客户端 Lua 依赖 Unity/C# 侧符号（logger / bit / phonestcore / G.timemgr …），
  由 STUBS 提供最小替身；替身只做「不报错」，不改变被测逻辑。
"""
import os

from lupa import lua53

#: decompile 出来的客户端 Lua 根目录（可用 OO_LUA_ROOT 覆盖，便于换版本）
LUA_ROOT = os.environ.get("OO_LUA_ROOT") or r"F:\haowancheng\archive\game_data\LuaAll_Decompiled2"


def _stub_source(now=1700000000):
    return r"""
-- 日志：客户端大量代码会 logger:info/error，替身只需不报错
logger = {
  info = function() end, warn = function() end, warning = function() end,
  error = function() end, debug = function() end, trace = function() end,
}
log = logger

-- 位运算：Lua 5.3 原生支持，客户端却按 5.1 的 bit 库写法 require("bit")
local bit_impl = {
  band = function(a, b) return a & b end,
  bor = function(a, b) return a | b end,
  bxor = function(a, b) return a ~ b end,
  bnot = function(a) return ~a end,
  lshift = function(a, n) return a << n end,
  rshift = function(a, n) return a >> n end,
  tobit = function(x) return x end,
  tohex = function(x) return string.format("%x", x) end,
}
package.preload["bit"] = function() return bit_impl end

-- ms 通信库（客户端日志走它）+ 日志等级常量
package.preload["phonestcore"] = function()
  return {
    log = logger, logger = logger,
    SPDLOG_LEVEL_DEBUG = 4, SPDLOG_LEVEL_INFO = 3,
    SPDLOG_LEVEL_WARN = 2, SPDLOG_LEVEL_ERROR = 1, SPDLOG_LEVEL_CRITICAL = 0,
  }
end

-- 日志子系统：只替身「取 logger」这一层，被测的 PropertySync 逻辑保持真身
package.preload["Core.Log.LoggerManager"] = function()
  return {
    getLogger = function() return logger end,
    setLevel = function() end, setEngineLevel = function() end,
    setAppendLuaTraceback = function() end,
    DEBUG = 4, INFO = 3, WARN = 2, ERROR = 1, CRITICAL = 0,
  }
end

-- 全局时钟：测试可注入固定时间，避免用例随真实时间漂移
_G.G = { timemgr = {
  NowI = function() return NOW end,
  Now = function() return NOW * 1000 end,
} }

-- 基础设施白名单自动替身：客户端会 require 一些 C 扩展（bson/cjson/socket…）。
-- 只对白名单生效，GameCommon/Entities/Core 这些被测模块缺一个都必须报错 ——
-- 不能因为自动替身把真正的缺失盖住。
local AUTO_STUB = { bson = true, cjson = true, socket = true, ["UnityEngine"] = true,
                    ["System"] = true, ["dp"] = true, ["lfs"] = true }
local function _stub_table()
  return setmetatable({}, { __index = function(t, k)
    local f = function() return nil end
    rawset(t, k, f)
    return f
  end })
end
-- Lua 5.2+ 叫 package.searchers（5.1 才叫 package.loaders）
local _searchers = package.searchers or package.loaders
table.insert(_searchers, 1, function(name)
  local first = name:match("^[^%.]+")
  if AUTO_STUB[name] or AUTO_STUB[first] then
    return function() return _stub_table() end
  end
  return nil
end)

-- Lua 5.3 去掉了全局 unpack（客户端按 5.1 写法用），补回来
unpack = unpack or table.unpack

-- 客户端对 table 的扩展（Lua 5.3 没有 table.length）
function table.length(t)
  local n = 0
  for _ in pairs(t) do n = n + 1 end
  return n
end

function table.empty(t)
  return next(t) == nil
end
""".replace("NOW", str(int(now)))


def new_runtime(now=1700000000):
    """建一个加载了客户端 Lua 的运行时。"""
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    root = LUA_ROOT.replace("\\", "/")
    lua.execute(
        f'package.path = "{root}/?.lua;{root}/?/init.lua;" .. package.path',
    )
    lua.execute(_stub_source(now))
    return lua


def require(lua, module):
    """require 一个客户端模块，失败时抛出带模块名的异常（便于定位缺哪个 stub）。"""
    lua.execute('REQ_OK = nil')
    lua.execute(f'REQ_OK = pcall(require, "{module}")')
    ok = lua.globals().REQ_OK
    if not ok:
        # 再 require 一次，让 lupa 把真实错误抛出来
        lua.execute(f'local ok, err = pcall(require, "{module}"); if not ok then error(err, 0) end')
    return True


def boot_config(lua):
    """加载 generated/config.lua 并让客户端 PropertyParser 建好全部自定义类型声明。

    之后 CustomTypeFactory.createReadonly("<类型名>", ...) 就能用了。
    """
    root = LUA_ROOT.replace("\\", "/")
    lua.execute(f'Config = assert(loadfile("{root}/generated/config.lua"))()')
    lua.execute('local pp = require("Core.Common.PropertyParser"); pp.parseCustomTypes(Config)')
    return lua


def parse_as_client(lua, type_name, value, name="x"):
    """用**客户端自己的** CustomTypeFactory/CustomDict 解析一个 dict。

    返回 (ok, err)。这正是客户端登录时的解析路径：
      createReadonly -> CustomType.roinit -> CustomDict/CustomList:roinit
    缺 __id__、类型不对、键类型不对，都会在这里断言失败。
    """
    lua.globals()._oo_type = type_name
    lua.globals()._oo_name = name
    lua.globals()._oo_src = lua.table_from(value, recursive=True)
    lua.execute(r"""
local factory = require("Core.PropertySync.CustomTypeFactory")
local ok, err = pcall(function()
  return factory.createReadonly(_oo_type, _oo_name, _oo_src, {}, nil, false)
end)
_oo_res = { ok = ok, err = tostring(err) }
""")
    res = lua.globals()._oo_res
    return bool(res.ok), (res.err or "")


def parse_and_read(lua, type_name, value, fields=(), name="x"):
    """同上，但成功时把客户端对象上的字段读出来（验证「能解析」之外还「读得对」）。"""
    lua.globals()._oo_type = type_name
    lua.globals()._oo_name = name
    lua.globals()._oo_src = lua.table_from(value, recursive=True)
    lua.globals()._oo_fields = lua.table_from(list(fields))
    lua.execute(r"""
local factory = require("Core.PropertySync.CustomTypeFactory")
local ok, err = pcall(function()
  local obj = factory.createReadonly(_oo_type, _oo_name, _oo_src, {}, nil, false)
  local out = {}
  for _, f in ipairs(_oo_fields) do out[f] = obj[f] end
  _oo_obj = out
end)
_oo_res = { ok = ok, err = tostring(err) }
""")
    res = lua.globals()._oo_res
    if not res.ok:
        return False, (res.err or ""), None
    picked = {}
    obj = lua.globals()._oo_obj
    for f in fields:
        picked[f] = obj[f]
    return True, "", picked
