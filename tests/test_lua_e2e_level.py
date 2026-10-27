#!/usr/bin/env python
"""H3 玩家等级：同源 Lua E2E（跑客户端自己的 AvatarLevelComp）。

## 这是什么

用 lupa 内嵌 **Lua 5.3**（与游戏 xLua 同版本）加载 archive 里 decompile 出来的
**同一份客户端 Lua**，把服务端产出的 `levelup_awards` / `level` / `exp`
喂给客户端**自己的** `AvatarLevelComp` 方法跑一遍。

不是 mock，也不复刻逻辑：`require("Entities.AvatarComponent.AvatarLevelComp")`
拿到的是客户端真身，跑的是它自己的 `hasAvailableLevelupAward()`。

## 为什么非要做这一层

写了单元测试之后仍然踩到一次真 bug（2026-10-28）：

    avatarlevelcomp.lua:36-45
      if tableData[i].awardItem and self.levelup_awards[i] == 0 then return true end

客户端判的是 **== 0**，不是「不等于 1」。
服务端原来「领了才写 1」，未领的键根本不存在（nil）→ 红点永远不亮。
而 `playerphasepanel` 那条路径用的是 `== 1`（nil 也判未领，能正常显示）——
**两处判定不一致，只有红点坏掉**，是所有失败模式里最难发现的一种。

单元测试复刻逻辑时也会写成 `!= 1` 而漏掉它；跑真 Lua 才当场暴露。

## 判定

  - 服务端产出的 levelup_awards 必须让客户端 hasAvailableLevelupAward() == true（未领）
  - 领完必须让它 == false（已领）
  - 满级 / 无奖励等级不能让客户端报错
  - DataLoader 读到的 LevelExpData 行数、expNeed 必须与服务端一致（同源数据一致性）

用法: python -m tests.test_lua_e2e_level
"""
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_lv_lua_")
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_lv_lua_ulog_"), "unknown_requests.tsv"))

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
    """建运行时 + 把客户端那两个全局依赖补上。

    `AvatarLevelComp` 用的是**全局** `DataLoader`（不是 require 进来的），
    而客户端的 DataLoader 在 `Helper/DataLoader`（老名字 `Core.Common.DataLoader`）。
    这两点都得按客户端真实情况补齐，否则跑不起来——补齐本身也是取证。
    """
    from tests.lua_harness import new_runtime  # noqa: PLC0415

    lua = new_runtime()
    lua.execute(r"""
package.preload["Core.Common.DataLoader"] = function() return require("Helper.DataLoader") end
DataLoader = require("Helper.DataLoader")
AVATAR_LEVEL = require("Entities.AvatarComponent.AvatarLevelComp")
""")
    return lua


def _make_comp(lua, level_no, exp, awards):
    """在用真组件造一个实例（只塞组件方法真正读的字段）。"""
    lua.globals()._lv = int(level_no)
    lua.globals()._exp = int(exp)
    lua.globals()._awards = lua.table_from(awards, recursive=True)
    lua.execute(r"""
local LV = AVATAR_LEVEL
local obj = setmetatable({}, { __index = LV })
obj.level = _lv
obj.exp = _exp
obj.levelup_awards = _awards
obj.consume_vit = 0
OO_COMP = obj
""")
    return lua.globals().OO_COMP


def _call(lua, method):
    """调组件方法，返回 (ok, value_or_err)。"""
    lua.globals()._m = method
    lua.execute(r"""
OO_OK, OO_VAL = pcall(function() return OO_COMP[_m](OO_COMP) end)
OO_VAL = tostring(OO_VAL)
""")
    ok = bool(lua.globals().OO_OK)
    val = lua.globals().OO_VAL
    if ok and val == "true":
        return True, True
    if ok and val == "false":
        return True, False
    return ok, val


def _awards_from_props(props):
    """把服务端 props 里的 levelup_awards 转成 Lua 能吃的 {[level]=0/1}。"""
    raw = props.get("levelup_awards") or {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, int):
            out[k] = int(v)
        elif isinstance(k, str) and k.isdigit():
            out[int(k)] = int(v)
    return out


def main():
    from tests.lua_harness import LUA_ROOT  # noqa: PLC0415

    if not os.path.isdir(LUA_ROOT):
        print("跳过：找不到客户端 Lua 目录 " + LUA_ROOT)
        return 0

    from game_server import level, ms  # noqa: PLC0415
    from game_server import player_state as ps

    print("\n[1] 客户端 Lua 5.3 能加载真组件")
    lua = _boot()
    check("运行时是 Lua 5.3", "5.3" in lua.eval("_VERSION"), lua.eval("_VERSION"))
    check("AvatarLevelComp 加载成功", lua.eval("type(AVATAR_LEVEL)") == "table")
    check("hasAvailableLevelupAward 是真函数",
          lua.eval("type(AVATAR_LEVEL.hasAvailableLevelupAward)") == "function")

    print("\n[2] 同源数据一致性：客户端 DataLoader 与服务端读的是同一张表")
    n_lua = int(lua.eval(r"(function() local d = DataLoader:getLevelExpData()"
                         r" local n=0 for _ in pairs(d) do n=n+1 end return n end)()"))
    check("客户端 LevelExpData 行数 == 服务端 max_level()",
          n_lua == level.max_level(), f"lua={n_lua} py={level.max_level()}")
    need1_lua = int(lua.eval("DataLoader:getLevelExpData()[1].expNeed"))
    check("客户端 expNeed[1] == 服务端 exp_need(1)",
          need1_lua == level.exp_need(1), f"lua={need1_lua} py={level.exp_need(1)}")

    print("\n[3] ★ 红点契约：未领必须显式 0（这是本套件存在的理由）")
    # 先证明「nil 会让客户端判未领失败」，这样测试才有牙
    _make_comp(lua, 2, 0, {})
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("★ 反例：awards[2]=nil 时客户端红点为 false（服务端不补 0 就坏在这）",
          ok and val is False, (ok, val))

    _make_comp(lua, 2, 0, {2: 0})
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("★ 正例：awards[2]=0 时客户端红点为 true", ok and val is True, (ok, val))

    _make_comp(lua, 2, 0, {2: 1})
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("已领（=1）时红点为 false", ok and val is False, (ok, val))

    print("\n[4] 端到端：服务端升级 -> 客户端红点亮 -> 领奖 -> 红点灭")
    props = ms.conv_int_keys(json.loads(json.dumps(ms.AVATAR_INIT_DICT.get("__Properties__") or {})))
    st = ps.get_state(props, uid="lvlua")
    st.is_new = False
    st.props["exp"] = level.exp_need(1)
    res = level.upgrade(st)
    check("服务端升级成功", res.ok, res.reason)

    awards = _awards_from_props(st.props)
    check("服务端产出的 awards 里 2 是显式 0", awards.get(2) == 0, awards)
    _make_comp(lua, st.props.get("level"), st.props.get("exp"), awards)
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("★ 客户端（真 Lua）认为有奖励可领", ok and val is True, (ok, val))

    level.grant_level_award(st, 2)
    awards2 = _awards_from_props(st.props)
    _make_comp(lua, st.props.get("level"), st.props.get("exp"), awards2)
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("★ 领奖后客户端（真 Lua）认为没有可领的了", ok and val is False, (ok, val))

    print("\n[5] 等级/经验进度条：客户端能读到正确值")
    need = int(lua.eval("DataLoader:getLevelExpData()[2].expNeed"))
    check("客户端读到 Lv2 的 expNeed == 250", need == 250, need)
    check("服务端 exp_need(2) 与客户端一致", level.exp_need(2) == need,
          f"py={level.exp_need(2)} lua={need}")

    print("\n[6] 边界：满级 / 无奖励等级不能让客户端报错")
    _make_comp(lua, level.max_level(), 0, {})
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("满级时客户端不报错", ok, val)
    _make_comp(lua, 1, 0, {1: 0})
    ok, val = _call(lua, "hasAvailableLevelupAward")
    check("Lv1（无奖励）时客户端不报错且红点灭", ok and val is False, (ok, val))

    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
