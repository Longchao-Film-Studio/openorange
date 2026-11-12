#!/usr/bin/env python3
"""客户端视角探针：把服务端数据喂给客户端真身 Lua，问「面板能不能打开」。

## 为什么

实机 bug「角色详情页点不进去」在服务端日志里什么都没有：面板是纯客户端逻辑，
它读 GlobalData.Avatar 的一堆方法，任何一个抛 nil-index 都会让
ui.OpenPanel 的 setData 中断 -> 用户看到「点了没反应」。

这个探针不重写面板，而是加载 **archive 里同一份客户端 Lua** 的
Entities.AvatarComponent.* 组件，装到服务端登录 initDict 上，
然后按面板真实调用顺序逐个调用，把第一个抛错的方法和错误原文打出来。

用法:
    python tools/probe_client_view.py --load-only            # 只看组件能不能加载
    python tools/probe_client_view.py --uid wayway --char 9002
"""
import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# BDD 配置表根目录（与 archive/game_data/LuaAll_Decompiled2 并列）
BDD_ROOT = os.path.join(os.path.dirname(ROOT), "archive", "game_data", "BDD")

# 角色详情页会调到的组件（按 method -> file 反查得出，不是猜的）
NEEDED_COMPONENTS = (
    "Entities.AvatarComponent.AvatarCharacterComp",
    "Entities.AvatarComponent.AvatarFormationComp",
    "Entities.AvatarComponent.AvatarValueComp",
    "Entities.AvatarComponent.AvatarBagComp",
    "Entities.AvatarComponent.AvatarSigilComp",
    "Entities.AvatarComponent.AvatarFashionEquipComp",
    "Entities.AvatarComponent.AvatarFavorSpaceComp",
    "Entities.AvatarComponent.AvatarDragonCharmComp",
    "Entities.AvatarComponent.AvatarStateMachine.AvatarStateComp",
    "Entities.AvatarComponent.AvatarSystemGuideComp",
)

#: 客户端依赖的 C# 侧全局，测试环境没有；替身只负责「不报错」。
STUBS = r"""
-- CS：Unity 的 C# 桥。客户端只在几个地方取常量和静态方法，替身一律返回可索引/可调用。
local function permissive()
  local t = {}
  return setmetatable(t, {
    __index = function(self, k)
      local v = permissive()
      rawset(self, k, v)
      return v
    end,
    __call = function() return nil end,
  })
end
CS = permissive()

-- 配置表：客户端有两条取法，指向**同一批 Lua 模块**
--   1. DataTable("Common", "CharData")     —— C# 侧注入的全局
--   2. require("TableData.CommonData.X")   —— DataLoader 里全是这种
-- 反编译树里 tabledata/ 有 492 个真实模块，直接 require 就行 ——
-- 不要从 BDD json 另造一份：那会引入「字符串键 vs 数字键」等假差异。
OO_MISSING = {}
local EMPTY = setmetatable({}, { __index = function() return nil end })

function DataTable(db, name)
  local modname = "TableData." .. db .. "Data." .. name
  local ok, t = pcall(require, modname)
  if ok and type(t) == "table" then
    return t
  end
  if not OO_MISSING[modname] then
    OO_MISSING[modname] = true
    OO_MISSING[#OO_MISSING + 1] = modname
  end
  return EMPTY
end
DataMgr = {
  getChineseText = function(id) return tostring(id) end,
  dataTable = function(db, name) return DataTable(db, name) end,
}

-- 红点服务（C# 侧）：客户端只管往里塞状态
RDC = { SetRed = function() end }
RDCF = { getSystemMenuBtnRedFlag = function() return false end,
         getSystemMenuBtnRedName = function(id) return tostring(id) end }

-- 客户端对 table 的扩展（contains/append/removeByValue…）在 GameCommon 里
require("GameCommon.Helper.LuaExtension")
-- 客户端入口 include.lua:64 把常量表塞成全局
UIGlobalConst = require("UI.Base_Extensions.UIGlobalConst")
-- ★ include.lua:39-43 用 LogUtils 覆盖了全局 print / error。
--   客户端里的 error() 只打日志、不抛也不返回 ——
--   「加个 error 守卫」在客户端等于「加一句日志」，不能当崩溃点看待。
local LogUtils = require("Utils.LogUtils")
print = LogUtils.printLog
error = LogUtils.error

-- 其它 C 扩展：客户端 require 它们只为工具函数
package.preload["json"] = function()
  return { decode = function() return {} end, encode = function() return "{}" end }
end
package.preload["time"] = function() return { clock = function() return NOW end } end
package.preload["cmsgpack"] = function()
  return { pack = function() return "" end, unpack = function() return nil end }
end
AccessorNameList = {}

-- 客户端在入口处把这些常量表塞进全局（不是 require 进去的模块级 local），
-- 组件按全局名取（例：Battle/Public/Utils/AttrUtil.lua:343 用全局 BattlePublicConst）
BattlePublicConst = require("Battle.Public.Common.BattlePublicConst")
SystemConst = require("Common.SystemConst")
""".replace("NOW", "1700000000")


def strip_empty_slots(props):
    """把角色身上的空槽位哨兵删掉 —— 复现**修复前**的存档形状（负例用）。

    修复前服务端下发的是「只有 __id__ 的空 dict」；客户端
    avatarsigilcomp.lua:145 读到 nil 却拿它和 "" 比，于是当成已装备，
    下一步拿 nil gridId 崩掉角色详情页。删掉哨兵就能把这个 bug 复现出来，
    这样 tests/test_empty_item_slots.py 的断言才不是「永远绿」。
    """
    from game_server.property import empty_slots

    bag = props.get("characterBag") or {}
    for key, char in list(bag.items()):
        if key in ("__tp__", "__id__") or not isinstance(char, dict):
            continue
        for field, keys in empty_slots.slots().items():
            node = char.get(field)
            if not isinstance(node, dict):
                continue
            for slot in keys:
                node.pop(slot, None)


def boot(uid=None, strip_sentinels=False, props=None):
    """建运行时：客户端配置表 + 服务端 initDict 解析出的属性 + 组件方法。"""
    from tests.lua_harness import boot_config, new_runtime

    lua = new_runtime()
    boot_config(lua)
    lua.execute(STUBS)


    # 客户端 DataLoader 真身（面板/组件里大量用）
    lua.execute(r"""
package.preload["Core.Common.DataLoader"] = function()
  local dl = require("Helper.DataLoader")
  dl:init()
  return dl
end
DataLoader = require("Core.Common.DataLoader")
""")

    # 服务端 initDict -> 客户端属性对象（真实登录路径）
    if props is None:
        from game_server.ms.initdict import live_init_dict

        props = live_init_dict(uid=uid).get("__Properties__") or {}
    if strip_sentinels:
        strip_empty_slots(props)
    lua.globals()._oo_props = lua.table_from(props, recursive=True)
    lua.execute(r"""
local PPC = require("Core.PropertySync.PuppetPropertyComponent")
OO_PPC = PPC
local obj = setmetatable({}, { __index = PPC })
obj.__ClassType = { typeName = "ClientAvatar", __PropertyCallbacks__ = {} }
OO_BOOT_OK, OO_BOOT_ERR = pcall(function() obj:initProperty({ __Properties__ = _oo_props }) end)
OO_BOOT_ERR = tostring(OO_BOOT_ERR or "")
OO_PROPS = obj
""")
    if not lua.globals().OO_BOOT_OK:
        raise SystemExit("客户端 initProperty 失败: " + str(lua.globals().OO_BOOT_ERR))

    # 装组件方法：真身 class.Component 的方法表 + 上面解析出的属性
    lua.globals()._oo_mods = lua.table_from(list(NEEDED_COMPONENTS))
    lua.execute(r"""
local PPC = OO_PPC
OO_LOAD_ERR = {}
local classes = { PPC }
for _, m in ipairs(_oo_mods) do
  local ok, cls = pcall(require, m)
  if ok and type(cls) == "table" then
    table.insert(classes, cls)
  else
    table.insert(OO_LOAD_ERR, m .. " | " .. tostring(cls):sub(1, 200))
  end
end
-- 属性优先，其次按组件顺序找方法（与客户端 Class 的多组件组合等价）
local index = function(_, k)
  local props = rawget(_oo_avatar_self, "__Properties__")
  if props ~= nil then
    local v = props[k]
    if v ~= nil then return v end
  end
  for _, cls in ipairs(classes) do
    local v = cls[k]
    if v ~= nil then return v end
  end
  return nil
end
_oo_avatar_self = {}
local avatar = setmetatable(_oo_avatar_self, { __index = index })
local props = rawget(OO_PROPS, "__Properties__")
for k, v in pairs(props) do rawset(avatar, k, v) end
rawset(avatar, "__Properties__", props)
rawset(avatar, "__PropertiesCnt__", rawget(OO_PROPS, "__PropertiesCnt__"))
rawset(avatar, "__CustomPropertiesCache__", rawget(OO_PROPS, "__CustomPropertiesCache__"))
rawset(avatar, "__ClassType", rawget(OO_PROPS, "__ClassType"))
rawset(avatar, "logger", logger)
GlobalData = { Avatar = avatar }
-- 组件里写的是 require("Core.Client.GlobalData")，要把同一个表接上
require("Core.Client.GlobalData").Avatar = avatar
-- ClientUtils 里是 local G = require("G")，和测试垫的全局 G 不是同一张表
local Gmod = require("G")
if type(Gmod) == "table" and Gmod.timemgr == nil then Gmod.timemgr = G.timemgr end
OO_AVATAR = avatar
-- 组件生命周期：客户端先逐个 init(avtDict)，再 start()。
-- 少了 init，AvatarBagComp.itemData 之类就一直 nil（那是探针的假报错）
OO_START_ERR = {}
for _, cls in ipairs(classes) do
  if cls.init then
    local ok, err = pcall(cls.init, avatar, {})
    if not ok then table.insert(OO_START_ERR, "init " .. tostring(err):sub(1, 200)) end
  end
end
for _, cls in ipairs(classes) do
  if cls.start then
    local ok, err = pcall(cls.start, avatar)
    if not ok then table.insert(OO_START_ERR, "start " .. tostring(err):sub(1, 200)) end
  end
end
""")
    return lua


def call(lua, expr):
    """执行一段 Lua 表达式，返回 (ok, 值或错误原文)。"""
    lua.globals()._oo_expr = expr
    lua.execute(r"""
OO_CALL_OK, OO_CALL_V = xpcall(function()
  local chunk = assert(load("return " .. _oo_expr))
  return chunk()
end, function(e)
  return tostring(e) .. "\n" .. debug.traceback("", 2)
end)
OO_CALL_V = tostring(OO_CALL_V)
""")
    return bool(lua.globals().OO_CALL_OK), lua.globals().OO_CALL_V


#: 角色详情页打开时按真实顺序调到的 Avatar 方法（来源：组件文件反查，
#: 见 docs/根因-角色详情页打不开.md）。每条只传面板实际传的实参。
def panel_calls(char_id):
    return [
        ("角色列表 setData: 我的角色", "GlobalData.Avatar:getMyCharactersByFilter()"),
        ("角色列表 setData: 布阵", "GlobalData.Avatar:getFormationInfo(GlobalData.Avatar:getChooseIndex())"),
        ("角色列表 setData: 未拥有", "GlobalData.Avatar:getNotOwnCharactersByFilter()"),
        ("角色格子: 是否拥有", f"GlobalData.Avatar:isOwnCharacter({char_id})"),
        ("角色格子: 使用皮肤", f"GlobalData.Avatar:getUsingSkinId({char_id})"),
        ("角色格子: 战斗标签", f"GlobalData.Avatar:getCharBattleTags({char_id})"),
        ("角色格子: 等级", f"GlobalData.Avatar:getCharacterLevel({char_id})"),
        ("角色格子: 可升星", f"GlobalData.Avatar:getCharacterCanRisingStar({char_id})"),
        ("角色格子: 突破奖励红点", f"GlobalData.Avatar:hasAnyNotFetchedCharBreakReward({char_id})"),
        ("角色格子: 装备红点", f"GlobalData.Avatar:checkCharEquipRedPoint({char_id}, false)"),
        ("详情页 setData: initShowItems->Overview", "GlobalData.Avatar:getCharacterLevelProgress(" + str(char_id) + ")"),
        ("详情页 refreshYinHen", f"GlobalData.Avatar:getCharacterBreakLevel({char_id})"),
        ("详情页 refreshYinHen", f"GlobalData.Avatar:getMaxBreakLevel({char_id})"),
        ("详情页 refreshBaseInfo", f"GlobalData.Avatar:getCharacterPropertyShowValue({char_id}, 'Mastery')"),
        ("详情页 refreshBaseInfo", f"GlobalData.Avatar:getCharacterValue({char_id})"),
        ("详情页 refoint: 突破红点", f"GlobalData.Avatar:hasAnyNotFetchedCharBreakReward({char_id})"),
        ("详情页 红点: skill tab", f"GlobalData.Avatar:getCharOnceRedDotState({char_id}, SystemConst.CharOnceRedDotKey.SkillTab)"),
        ("详情页 红点: trendy", f"GlobalData.Avatar:checkCharEquipRedPoint({char_id})"),
        ("详情页 红点: yinhen", f"GlobalData.Avatar:getCharacterCanRisingStar({char_id})"),
        ("详情页 红点: skin", f"GlobalData.Avatar:hasCharNewSkin({char_id})"),
        ("详情页 红点: sigil tab", f"GlobalData.Avatar:getCharOnceRedDotState({char_id}, SystemConst.CharOnceRedDotKey.SigilTab)"),
        ("详情页 红点: 传记", f"GlobalData.Avatar:checkBiographyRedPoint({char_id})"),
        ("详情页 红点: 角色主页(赛季)", f"GlobalData.Avatar:CheckFavorSeasonRewardRedPoint({char_id})"),
        ("详情页 红点: 角色主页(事件)", f"GlobalData.Avatar:CheckFavorEventRedPoint({char_id})"),
        ("详情页 红点: 角色列表按钮 1", "GlobalData.Avatar:hasAnyCharacterCanRisingStar()"),
        ("详情页 红点: 角色列表按钮 2", "GlobalData.Avatar:getCharInFormationHasAnyNotFetchedCharBreakReward()"),
        ("详情页 红点: 角色列表按钮 3", "GlobalData.Avatar:checkAvatarFashionEquipRedPoint(false)"),
        ("详情页 角色主页入口: 状态机", "GlobalData.Avatar:getSMState()"),
        ("详情页 角色主页入口: 合法", f"GlobalData.Avatar:checkRoleHomeValid({char_id})"),
        ("详情页 换装入口", "GlobalData.Avatar:getCharFashionEquipInfo(" + str(char_id) + ")"),
        ("详情页 换装入口", "GlobalData.Avatar:getCharFashionEquipSuitInfos(" + str(char_id) + ")"),
        ("详情页 Sigil tab", f"GlobalData.Avatar:getCharWearingSigilUUID({char_id})"),
        ("详情页 Skill tab", f"GlobalData.Avatar:getCharSkillLevelByShowType({char_id}, 1, 1)"),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="wayway")
    ap.add_argument("--char", type=int, default=0)
    ap.add_argument("--load-only", action="store_true")
    ap.add_argument("--strip-sentinels", action="store_true",
                    help="删掉空槽位哨兵（复现「角色详情页点不进去」的旧形状）")
    args = ap.parse_args(argv)

    if not os.path.isdir(BDD_ROOT):
        print("找不到 BDD 目录: " + BDD_ROOT)
        return 1

    if args.load_only:
        from tests.lua_harness import boot_config, new_runtime

        lua = new_runtime()
        boot_config(lua)
        lua.execute(STUBS)
        for m in NEEDED_COMPONENTS:
            lua.globals()._m = m
            lua.execute("OO_OK, OO_ERR = pcall(require, _m)")
            print(("  OK  " if lua.globals().OO_OK else "  BAD ") + m,
                  "" if lua.globals().OO_OK else "| " + str(lua.globals().OO_ERR)[:160])
        return 0

    lua = boot(args.uid, strip_sentinels=args.strip_sentinels)
    errs = lua.globals().OO_LOAD_ERR
    if errs is not None:
        for i in range(1, len(errs) + 1):
            print("  组件加载失败 " + str(errs[i]))
    serrs = lua.globals().OO_START_ERR
    if serrs is not None:
        for i in range(1, len(serrs) + 1):
            print("  组件 start() 失败 " + str(serrs[i]))

    char_id = args.char or int(lua.eval(
        "(function() for k in pairs(GlobalData.Avatar.characterBag or {}) do return k end return 0 end)()"))
    print(f"uid={args.uid} charId={char_id}\n")

    nbad = 0
    for label, expr in panel_calls(char_id):
        ok, val = call(lua, expr)
        if ok:
            print(f"  ok    {label:<26} {expr.replace('GlobalData.Avatar:', '')} = {val[:70]}")
        else:
            nbad += 1
            print(f"  FAIL  {label:<26} {expr.replace('GlobalData.Avatar:', '')}")
            print(f"        -> {val[:900]}")
    print(f"\n失败 {nbad} 项")
    return 1 if nbad else 0


if __name__ == "__main__":
    sys.exit(main())
