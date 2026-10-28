"""客户端属性同步回放器（同源 Lua）。

## 为什么需要它

实机 bug：服务端把 exp 从 0 改成 100，但没发同步帧，客户端要重新登录才看到。
修这类问题之前必须先能回答一个问题：**客户端回放这串帧之后，到底看到什么？**

按属性名比对会误报（实测 4 例）：

    改动 space_p        -> 帧里是 spaceno / seqno / spaceid（子字段）
    改动 story_task_bag -> 帧里是 "400013"（容器键）

只有让客户端自己的解析器回放，才知道客户端的最终状态。

## 实现要点（都是踩出来的）

1. 用客户端真身 `PuppetPropertyComponent` 的 `__OpChange__` / `__OpAdd__` / `__OpDel__`。
2. 它自带的 `__index` 会调 `class.GetVtbl(tbl.__ClassType)`，而 `GetVtbl` 查的是
   `Core.Framework.Class` 内部的私有注册表 —— 外部造不出登记过的 classType
   （实测：`class.Component` / `class.Class` 都拿不到 vtbl，调用直接 C stack overflow）。
   所以这里自己实现一份等价的 `initProperty`，并给实例一个不查 vtbl 的 `__index`。
3. 自定义对象的字段存在 `_properties` 里，必须走 `__index` 读；`rawget` 拿不到。
4. `_oo_` 私有字段服务端不下发，`__tp__`/`__id__` 是元数据，比对时都要剔除。

## 用法

    from tests.lua_prop_replay import PropReplayer
    r = PropReplayer(ms.AVATAR_INIT_DICT["__Properties__"])
    r.apply_frames(frames)
    r.snapshot()
"""
from __future__ import annotations

from typing import Any

#: 转换时的环检测（自定义对象通过 _parent 反向引用整棵树）。
_SEEN: set[int] = set()

_LUA_BOOT = r"""
local PPC = require("Core.PropertySync.PuppetPropertyComponent")
local Factory = require("Core.PropertySync.CustomTypeFactory")

local function make_index(proto)
  return function(t, k)
    local props = rawget(t, "__Properties__")
    if props ~= nil and props[k] ~= nil then
      return props[k]
    end
    return proto[k]
  end
end

local function init_property(self, dict)
  self.__Properties__ = {}
  self.__PropertiesCnt__ = 0
  self.__CustomPropertiesCache__ = {}
  for name, value in pairs(dict.__Properties__) do
    if type(value) == "table" then
      local tp = value.__tp__
      if tp ~= nil then
        value.__tp__ = nil
        local ok, obj = pcall(function()
          return Factory.createReadonly(tp, name, value, self, nil, true)
        end)
        if ok and obj then
          obj:_attachToCache(self.__CustomPropertiesCache__)
          self.__Properties__[name] = obj
        end
      end
    else
      self.__Properties__[name] = value
    end
  end
end

function OO_NewReplayer(initProps)
  local obj = {}
  setmetatable(obj, {
    __index = make_index(PPC),
    __newindex = function(t, k, v) rawset(t, k, v) end,
  })
  rawset(obj, "__ClassType", { typeName = "OoReplayAvatar", __PropertyCallbacks__ = {} })
  init_property(obj, { __Properties__ = initProps })
  return obj
end

function OO_ApplyOp(obj, propertyId, opname, name, value)
  if opname == "change" then
    return PPC.__OpChange__(obj, propertyId, name, value, false)
  elseif opname == "add" then
    return PPC.__OpAdd__(obj, propertyId, name, value, false)
  elseif opname == "del" then
    return PPC.__OpDel__(obj, propertyId, name, false)
  end
  error("unknown op: " .. tostring(opname))
end
"""


#: 转换在 Lua 侧一次做完，再把纯数据交给 Python。
#:
#: 为什么不在 Python 里递归 lupa 表：
#:   - lupa 每次访问同一张 Lua 表都返回新的代理对象，`id()` 不稳定，做不了环检测
#:   - 自定义对象的字段在 `_properties` 里，内部还有 `_root`/`_owner` 反向引用
#:   - `v["_properties"]` 用 Python 下标取到的可能是外层同名字段（实测拿到空表）
#: 在 Lua 侧用 rawget + 显式递归，这些坑一次绕开。
_LUA_TO_PY = r"""
local function to_py(v, depth)
  if type(v) ~= "table" then return v end
  if depth > 12 then return "<deep>" end
  if SEEN[v] then return "<cycle>" end
  SEEN[v] = true
  local inner = rawget(v, "_properties")
  if type(inner) == "table" then v = inner end
  local out = {}
  for k, x in pairs(v) do
    local key = tostring(k)
    if string.sub(key, 1, 2) ~= "__" then
      out[key] = to_py(x, depth + 1)
    end
  end
  SEEN[v] = nil
  return out
end

function OO_ToPy(v)
  SEEN = {}
  return to_py(v, 0)
end
"""


def _plain(v: Any) -> Any:  # noqa: ANN401
    """把 Lua 返回的表递归转成 Python 原生 dict/list（lupa 不会自动转）。"""
    if hasattr(v, "items") and not isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _to_py(v: Any) -> Any:  # noqa: ANN401
    """把客户端对象转成 Python 纯数据（走 Lua 侧递归，见 _LUA_TO_PY）。"""
    if isinstance(v, (int, float, str, bool)) or v is None:
        return int(v) if isinstance(v, float) and float(v).is_integer() else v
    if not hasattr(v, "items"):
        return v
    return _REPLAYER_LUA.globals().OO_ToPy(v) if _REPLAYER_LUA is not None else v


#: 当前活跃的 Lua 运行时（_to_py 需要用它执行 Lua 侧转换）。
_REPLAYER_LUA = None




class PropReplayer:
    """把服务端帧回放到客户端解析器里，得到「客户端看到的状态」。"""

    def __init__(self, init_props: dict, *, now: int = 1700000000) -> None:
        from .lua_harness import boot_config, new_runtime  # noqa: PLC0415

        global _REPLAYER_LUA  # noqa: PLW0603

        lua = new_runtime(now=now)
        boot_config(lua)
        lua.execute(_LUA_BOOT)
        lua.execute(_LUA_TO_PY)
        lua.globals()._oo_init = lua.table_from(init_props, recursive=True)
        lua.execute("OO_OBJ = OO_NewReplayer(_oo_init)")
        self._lua = lua
        _REPLAYER_LUA = lua

    def apply(self, property_id: int, opname: str, name: str, value: Any) -> None:  # noqa: ANN401
        """应用一帧。opname ∈ {"change", "add", "del"}。"""
        lua = self._lua
        lua.globals()._pid = int(property_id)
        lua.globals()._opname = opname
        lua.globals()._name = str(name)
        if isinstance(value, dict):
            lua.globals()._val = lua.table_from(value, recursive=True)
        else:
            lua.globals()._val = value
        lua.execute("OO_ApplyOp(OO_OBJ, _pid, _opname, _name, _val)")

    def apply_frames(self, frames: list) -> None:
        """回放一串 (propertyId, opname, name, value) 元组。"""
        for pid, opname, name, value in frames:
            self.apply(pid, opname, name, value)

    def snapshot(self) -> dict[str, Any]:
        """客户端最终状态：{根属性名: 值}。

        lupa 的 Python 属性访问对 `__dunder__` 形式会走 Python 协议而不是 Lua 表
        （实测 AttributeError: type object object has no attribute __getattr__），
        所以必须用 Lua 侧 rawget 取属性容器。
        """
        payload = self._lua.eval("OO_ToPy(rawget(OO_OBJ, '__Properties__'))")
        raw = _plain(payload)
        out: dict[str, Any] = {}
        for k, v in raw.items():
            name = str(k)
            if name.startswith("_oo_"):
                continue
            out[name] = v
        return out
