#!/usr/bin/env python
"""A/B/C 契约：同源 Lua E2E（跑客户端自己的属性解析 + 面板逻辑）。

## 这是什么

用 lupa 内嵌 **Lua 5.3**（与游戏 xLua 同版本）加载 archive 里 decompile 出来的
**同一份客户端 Lua**，把服务端走完登录流程后产出的 initDict 交给
客户端**自己的** `Core.PropertySync` 去解析，再用真面板代码读它。

不是 mock，也不复刻逻辑：

    Core.PropertySync.PuppetPropertyComponent:initProperty   <- 客户端真身
    Core.PropertySync.CustomTypeFactory.createReadonly        <- 客户端真身
    UI.Panel.DirectorTitleV3Panel...node_daily_task_itemGroup <- 客户端真身

## 为什么非要做这一层

`title_daily_task_cache` 缺失时的表现是「点面板没反应」，
而单元测试再怎么断言「服务端有下发这个 key」，也证明不了
**客户端解析出来的对象真能被面板读到**。

2026-10-28 的教训是活生生的：`levelup_awards` 的单元测试全绿，
但客户端判的是 `== 0` 而不是「不等于 1」，跑真 Lua 才暴露（见 tests/test_lua_e2e_level.py）。

## 判定（关键是负例）

  [1] 客户端能加载 PropertySync / 属性组件
  [2] ★ 负例：initDict 里缺 title_daily_task_cache 时，
      客户端面板的 refreshState 必须抛「attempt to index a nil value」
      —— 这就是当初的实机 bug，必须能被复现
  [3] 正例：走完登录流程的 initDict 里，面板 refreshState 不抛错
  [4] is_first_day 的值必须能被客户端读到（!= nil）
  [5] levelup_awards 在客户端解析后必须可索引

用法: python -m tests.test_lua_e2e_prop_origin
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_origin_lua_")
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_origin_lua_ulog_"), "unknown_requests.tsv"))

_ok = 0
_bad: list[str] = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _bad.append(name)
        print(f"  FAIL  {name}" + (f" | {detail}" if detail else ""))


def _boot():
    """建运行时 + 加载客户端真身模块。

    `DataLoader` 是**全局**（`GlobalData` 也是），客户端就是这么用的，
    必须按真实情况补齐，否则跑不起来 —— 补齐本身也是取证。

    ★ 只加载 `Core.PropertySync` —— 那是客户端登录解析属性的真身。
      `UI.Panel.*` 面板类**加载不了**（依赖整个 Unity 的全局 `CS` 桥，
      `UI/Base/FontManager.lua:3` 一 require 就崩），所以面板那一层
      用「从源码里抠出真实语句再执行」的办法（见 _panel_refresh_state），
      保证被测代码仍然是客户端原文，不是复刻。
    """
    from tests.lua_harness import boot_config, new_runtime  # noqa: PLC0415

    lua = new_runtime()
    lua.execute(r"""
package.preload["Core.Common.DataLoader"] = function() return require("Helper.DataLoader") end
DataLoader = require("Helper.DataLoader")
OO_PPC = require("Core.PropertySync.PuppetPropertyComponent")
""")
    # ★ 必须先 boot_config：客户端要先解析 config.lua 注册那 345 个自定义类型，
    #   CustomTypeFactory 才认得 __tp__。少了这一步，只有 71 个原始类型能过，
    #   其余 149 个表值属性会被 createReadonly 返回 nil 然后**静默丢弃**
    #   （initProperty:56-60 只打一条 logger:error，不抛错）。
    #   实测踩过：属性数从 220 掉到 71，而 initProperty 仍然返回成功。
    boot_config(lua)
    return lua


def _parse_avatar(lua, props):
    """用客户端真身 PuppetPropertyComponent:initProperty 解析 __Properties__。

    这是客户端登录时**真实走的那条路径**：
        ClientAvatar:init -> initProperty(dict)
        -> 按 value.__tp__ 建 CustomTypeFactory.createReadonly 对象
    返回 (ok, err)。缺 __tp__ / 类型不存在 / 断言失败都会在这里暴露。
    """
    lua.globals()._oo_props = lua.table_from(props, recursive=True)
    lua.execute(r"""
local PPC = OO_PPC
local obj = setmetatable({}, { __index = PPC })
-- initProperty 内部会读 self.__ClassType.typeName（只用于报错信息）
obj.__ClassType = { typeName = "ClientAvatar", __PropertyCallbacks__ = {} }
OO_OK, OO_ERR = pcall(function() obj:initProperty({ __Properties__ = _oo_props }) end)
OO_OBJ = obj
OO_ERR = tostring(OO_ERR or "")
""")
    ok = bool(lua.globals().OO_OK)
    err = lua.globals().OO_ERR or ""
    return ok, err


#: 客户端面板里读 title_daily_task_cache 的那一行（唯一读点）。
#:
#: ★ 为什么是「从源码抠一行」而不是 require 整个面板：
#:   `UI/Panel/.../node_daily_task_itemGroup.lua` 依赖 `UI.Base.GroupBase`
#:   -> `UI.Base.UIBase` -> `UI.Base.FontManager`，而 FontManager:3 直接索引
#:   全局 `CS`（Unity C# 桥），测试环境没有，一 require 就崩。
#:   所以面板那一层用源码里的**原始语句**执行 —— 被测代码仍是客户端原文，
#:   不是我们复刻的版本。抠出来时校验它和预期完全一致，客户端改了就红。
#: 面板源文件（相对 decompile 根目录）。
PANEL_FILE = "UI/Panel/DirectorTitleV3Panel/node_daily_task_item/node_daily_task_itemGroup.lua"


def _panel_source():
    """读客户端面板源码；找不到返回 None（测试会报出来，不静默跳过）。"""
    from tests.lua_harness import LUA_ROOT  # noqa: PLC0415

    path = os.path.join(LUA_ROOT, *PANEL_FILE.split("/"))
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


PANEL_SNIPPET = "local firstDay = GlobalData.Avatar.title_daily_task_cache.is_first_day ~= 1"


def _panel_refresh_state(lua, avatar_obj, with_cache):
    """执行客户端 refreshState 里的那一行，看它在缺属性时会不会崩。

    `node_daily_task_itemGroup.lua:106`（旧版本是 :86）：

        local firstDay = GlobalData.Avatar.title_daily_task_cache.is_first_day ~= 1

    属性是 nil -> `attempt to index a nil value` -> `initEx` 抛错
    -> `closePanelEx` -> 面板消失 -> 用户看到「点了没反应」。
    """
    lua.globals()._oo_avatar = avatar_obj if with_cache else _empty_avatar(lua)
    lua.globals()._oo_snippet = PANEL_SNIPPET
    lua.execute(r"""
GlobalData = { Avatar = _oo_avatar }
OO_OK, OO_ERR = pcall(function()
  local chunk = load(_oo_snippet)
  if chunk == nil then error("snippet 编译失败") end
  return chunk()
end)
OO_ERR = tostring(OO_ERR or "")
""")
    return bool(lua.globals().OO_OK), lua.globals().OO_ERR or ""


def _empty_avatar(lua):
    """造一个「服务端没下发 title_daily_task_cache」的实体。"""
    lua.execute("OO_EMPTY = setmetatable({}, { __index = function() return nil end })")
    return lua.globals().OO_EMPTY


def main():
    from tests.lua_harness import LUA_ROOT  # noqa: PLC0415

    if not os.path.isdir(LUA_ROOT):
        print("跳过：找不到客户端 Lua 目录 " + LUA_ROOT)
        return 0

    from game_server import prop_origin as origin  # noqa: PLC0415
    from game_server.ms import live_init_dict  # noqa: PLC0415

    print("\n[1] 客户端 Lua 5.3 能加载真组件")
    lua = _boot()
    check("运行时是 Lua 5.3", "5.3" in lua.eval("_VERSION"), lua.eval("_VERSION"))
    check("PuppetPropertyComponent 加载成功", lua.eval("type(OO_PPC)") == "table")
    check("initProperty 是真函数", lua.eval("type(OO_PPC.initProperty)") == "function")
    # ★ 抠出来的那一行必须**确实存在于客户端源码里**，而且逐字一致。
    #   客户端改了这行代码，这里就红 —— 防止测试慢慢变成测一个不存在的语句。
    src = _panel_source()
    check("面板源码里能找到读取语句", src is not None, PANEL_FILE)
    hits = [ln for ln in src.splitlines() if "title_daily_task_cache" in ln]
    check("读取语句唯一", len(hits) == 1, hits)
    check("语句与预期逐字一致", len(hits) == 1 and hits[0].strip() == PANEL_SNIPPET,
          hits[0].strip() if hits else None)

    print("\n[2] ★ 负例：缺少 title_daily_task_cache 时客户端必须崩（复现实机 bug）")
    ok, err = _panel_refresh_state(lua, None, with_cache=False)
    check("refreshState 抛错", not ok, err)
    check("错误信息是「索引 nil」（和实机日志一致）",
          "nil value" in str(err), str(err)[:160])

    print("\n[3] 正例：走完登录流程的 initDict 里客户端能解析、面板不崩")
    uid = "t_lua_origin"
    init = live_init_dict(uid=uid)
    props = init.get("__Properties__") or {}
    print(f"        （initDict 有 {len(props)} 个属性）")

    ok, err = _parse_avatar(lua, props)
    check("客户端 initProperty 解析全部属性成功", ok, str(err)[:300])
    n_parsed = int(lua.eval("(function() local n=0 for _ in pairs(OO_OBJ.__Properties__ or {}) "
                            "do n=n+1 end return n end)()"))
    check("客户端建立的属性数 == initDict 属性数", n_parsed == len(props),
          f"lua={n_parsed} py={len(props)}")

    # ★ 必须用 rawget 读：PuppetPropertyComponent 的 __index 有自定义实现
    #   （puppetpropertycomponent.lua:16 查 __ClassType.__PropertyCallbacks__），
    #   而实例的 __ClassType 是我们垫的替身，走元方法会崩。
    #   客户端的属性本身存在 __Properties__ 这张表里，rawget 才是它的真实位置。
    node = lua.eval("rawget(OO_OBJ.__Properties__, 'title_daily_task_cache')")
    check("客户端解析出 title_daily_task_cache 对象", node is not None, node)
    # ★ 自定义对象的字段存在 _properties 里，必须走 __index 元方法读；
    #   rawget 拿不到（实测踩过：rawget 返回 nil，而 node.is_first_day 是 0）。
    first_day = lua.eval(
        "(function() local n = rawget(OO_OBJ.__Properties__, 'title_daily_task_cache') "
        "if n == nil then return nil end return n.is_first_day end)()")
    check("is_first_day 能被读到（不是 nil）", first_day is not None, first_day)
    check("is_first_day 是整数", isinstance(first_day, (int, float)), type(first_day))

    ok, err = _panel_refresh_state(lua, lua.globals().OO_OBJ, with_cache=True)
    check("★ 面板 refreshState 不再抛错（这个 bug 修没了）", ok, str(err)[:200])

    print("\n[4] street_level_info 同样能被客户端解析")
    sli = lua.eval("rawget(OO_OBJ.__Properties__, 'street_level_info')")
    check("客户端解析出 street_level_info", sli is not None, sli)

    print("\n[5] levelup_awards 在客户端可索引（红点判定依赖）")
    awards_ok = lua.eval(
        "(function() "
        "local P = OO_OBJ.__Properties__ ; "
        "local a = rawget(P, 'levelup_awards') ; "
        "if a == nil then return false end ; "
        "local lv = rawget(P, 'level') or 1 ; "
        "for i = 1, lv do local v = a[i] ; if v ~= nil then return true end end ; "
        "return true "
        "end)()")
    check("levelup_awards 可索引不抛错", bool(awards_ok), awards_ok)

    print("\n[6] 派生属性不在存档里，但客户端照样能读到")
    derived = origin.derived_props()
    missing_in_client = []
    for name in derived:
        val = lua.eval(f"rawget(OO_OBJ.__Properties__, {name!r})")
        if val is None:
            missing_in_client.append(name)
    check("所有 derived 属性客户端都能读到（不因不存盘而缺失）",
          not missing_in_client, missing_in_client)

    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
