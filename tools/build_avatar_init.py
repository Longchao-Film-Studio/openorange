#!/usr/bin/env python3
"""tools/build_avatar_init.py —— 生成 createChannelEntity 用的 initDict

数据来源（全部逆向自客户端，无猜测）：

1. game/GuiLongchao_Data/PersistentPath/Patch/Lua/generated/config.lua 的
   AvatarProperties 段 —— 玩家实体属性的权威声明，每条形如
   { propType, default, aoiscope, persist }。

2. 同文件的 Custom 段 —— 345 个自定义类型（206 结构体 + 139 字典），
   用来递归展开结构体属性。

3. 各组件 PropertyCallbacks 里被 pairs()/ipairs() 遍历的属性名 ——
   这些如果为 nil，客户端会抛
   "bad argument #1 to 'pairs' (table expected, got nil)"。

关键结构规则（逆向自 Core/PropertySync/PuppetPropertyComponent.lua:initProperty
和 Core/PropertySync/CustomDict.lua:roinit）：

  * 顶层属性必须带 __tp__ = 类型注册名，initProperty 会摘掉它再建对象。
  * **嵌套属性绝对不能带 __tp__** —— 嵌套的 roinit 会 pairs(value) 遍历，
    把 __tp__ 当成一个子属性名，然后拿 "DrawProp" 这种字符串当表用，直接崩：
      CustomDict.lua:485 in function roinit
        name = "__tp__"   value = "DrawProp"
    嵌套值的类型由客户端的 propDeclares 决定，我们只提供数据 + __id__。
  * 每个自定义对象都要有唯一的 __id__（roinit 里 assert(id ~= nil)）。

用法：
    python tools/build_avatar_init.py
输出：
    game_server/avatar_init.json
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "game_server", "avatar_init.json")

# config.lua 来自解包语料（游戏里的 Lua 是 .bytes 打包的）：
#   python tools/unpack_lua.py   -> archive/game_data/LuaFull/luac
#   python tools/decompile_lua.py -> archive/game_data/LuaFull/src
# 可用环境变量 OO_LUA_SRC 覆盖。
_LUA_SRC = os.environ.get("OO_LUA_SRC") or os.path.join(
    ROOT, "..", "archive", "game_data", "LuaFull", "src")
CONFIG_LUA = os.path.join(_LUA_SRC, "generated", "config.lua")

SIMPLE = {"int", "string", "double", "boolean"}
DEF = {"int": 0, "string": "", "double": 0.0, "boolean": False}

# 「客户端读不到就崩、需要预置」的属性知识全部在 tools/client_contract.py：
#   SERVER_PROPS  预置**自定义对象**的属性 -> 类型（38 条）
#   SIMPLE_PROPS  预置**标量初值**的属性 -> 默认值
#
# ★ 2026-10-28 起本文件不再持有这两张表。原因（不是整理代码洁癖）：
#   tests/test_avatar_prop_coverage.py 原本自己复制了一份豁免清单和扫描逻辑，
#   生成器一份、测试一份 —— 两份会漂移，而漂移的后果是「测试绿了、实机崩了」。
#   历史上这块叫 REQUIRED，38 条每条背后都是一次实机崩溃
#   （dungeonView / title_daily_task_cache / singleMatchInfo ...），
#   每次都是「发现缺失 -> 手工加一条」，典型的补丁驱动。
#   现在改成：发现（扫客户端读点）-> 解析（类型从哪来）-> 生成，全部走 client_contract。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client_contract import required_simple_props as _required_simple_props  # noqa: E402
from client_contract import required_types as _required_types  # noqa: E402

REQUIRED = _required_types()
SIMPLE_PROPS = _required_simple_props()

# 初始编队：一队 3 个空位（主角在取名时由 game_server/protagonist.py 创建并填入）。
# CustomList:roinit（core/propertysync/customlist.lua:398）是
#   for k, v in ipairs(value) do ...
# 所以 msgpack 里要用整数键 1..n，对应生成器的 "#1" 写法。

# 系统入口解锁表。
# entities/avatarcomponent/avatarsystemguidecomp.lua:86-94：
#   function AvatarSystemGuideComp:isSysEntryUnlock(sysId)
#     if sysId == nil then return true end
#     if self._gmSysUnlock then return true end
#     return self.system_p.unlocked_system[sysId] == true
#   end
# 不填的话 M 键地图、ESC 菜单里的角色/队伍、左上角等级全都不显示（实测）。
# 私服直接全解锁。id 来自 SystemConst.AllSystemCatalogue（common/systemconst.lua:184）。

# 系统入口的第二层门槛（AllSystemCatalogueData 的 unlockType/unlockParam）。
# ui/panel/streetv2panel/node_menu_item/node_menu_itemgroup.lua:231-256：
#   unlockType=1(Task)      -> getSystemTaskState(taskId) == Finished
#                              -> done_tasks[TaskType][taskId] == true
#   unlockType=2(StoryTask) -> getTaskStateByTaskID(taskId) == Finished
#                              -> done_story_tasks[taskId] == true
#   unlockType=3(Level)     -> level >= unlockParam
# 不满足就弹 Toast textID=1（就是用户看到的「暂未开放」）。
# 实测的门槛：地图/背包/任务/图鉴/教学/客服/攻略要任务 1，
#             角色/商城/龙隐门/成就/好友/活动要剧情任务 1005/1007/1010，
#             编队要任务 15，播客/昼夜/基础课程/玩法总览要等级 4~7。
# ★ 系统解锁 / 任务全完成 / 等级拉高 这些**脚手架**已经搬到
#   game_server/scaffold.py，改成运行时可开关的覆盖层（默认关闭），
#   不再烤进 avatar_init.json。原因见那个文件的头部注释。
#
#   本文件只负责「客户端声明的属性默认值 + 新号初始数据」—— 这些是合法的、
#   真实服务端也会给的东西，长期保留。

STARTER_FORMATION = {
    "character_list": [
        # ★ 必须是 3 个格子，空位填 0，不能只给 1 个。
        # streetv2panel.lua:1311-1341 initFormation：
        #   for i = 1, 3 do
        #     local charId = formation.value[i]
        #     ...
        #     if member and 0 < charId then   <- i=2 时 charId 是 nil -> 0 < nil 抛错
        # 只给 1 个元素的话，i=2 就是 nil，StreetV2Panel:initEx 整个被打断，
        # 于是面板建了又关、零 HUD。
        {"name": "", "value": [0, 0, 0]},
    ],
}

# 客户端要初值的简单类型属性 -> 默认值（数据在 tools/client_contract.py 的 SIMPLE_PROPS）。
#
# ★ 2026-10-28 起数据搬走，和 SERVER_PROPS 一个理由：生成器和守卫测试共用一份，
#   不再各存一份。这里只剩「取值」这一步。
#   这块历史上有一次实机 bug：character_choosen_char 写死 9001 导致新号
#   一出生就有男导演，选女号后两个导演并存。默认值本身有语义，理由都写在
#   client_contract.SIMPLE_PROPS 的条目上。

# 需要预置的字典条目：属性名 -> {条目键: 值}。
#
# 键以 "#" 开头表示**整数键**。依据 core/propertysync/customdict.lua:464：
#   for k, v in pairs(value) do ... self._properties[k] = v
# roinit 直接把 msgpack 解出来的键原样拷进 _properties，**不做类型转换**。
# 所以 BooleanDictKeyInt 这类 __IntTypeKey__=true 的字典，
# 键在 msgpack 里必须真的是整数，写成字符串 "1" 的话 story_finished[1] 永远是 nil。
SEED = {
    # entities/clientavatar.lua:180 CheckEnterPrologue():
    #   if not self.story_finished[1] then <走序章 StartGame> else LoginSuccess end
    #
    # ★ 2026-10-21 改：以前这里是 {"#1": True}，为了绕过序章直接进街区。
    #   现在序章副本（Cmd_Avatar_StartGame_CS + spaceInfo）已经实现了，
    #   所以**默认留空**，让新号真的走序章副本。
    #   想要「老玩家回流」直接进街区时，用脚手架（--unlock-all），
    #   scaffold.py 会把 story_finished[1] 置真。
    "story_finished": {},
}

# 初始角色（1-1 角色数据模型）。
#
# 证据：
#   TableData.CommonData.CommonConstData.json
#     male_player_char = 9001, female_player_char = 9002
#     Default_PlayerName = "未具名导演"
#   TableData.CommonData.CharData.json
#     9001 -> charName "瞳", DefaultSkin 900101, charType 1（主角）
#     9002 -> charName "瞳", DefaultSkin 900201
#   stage/streetstage.lua:167-185 checkIsCanEnterStreet() 要求：
#     story_finished[1] 为真、player_name 非空、gender 非 0、
#     characterBag[male_player_char] 或 characterBag[female_player_char] 存在
# ★ 2026-10-27：新号**不给任何角色**（空 characterBag）。
# 取名时 game_server/protagonist.py 按性别创建唯一主角（男/女），
# 并把编队、character_choosen_char 一起对齐；登录时做互斥自愈。
# 依赖：checkIsCanEnterStreet 只要求「至少一个主角存在」，
# 而取名提交（gender!=0 且有主角）之后才可能进街区，顺序是自洽的。
STARTER_CHARS = {
    "characterBag": {},
}

# 初始货币（1-2 背包与货币）。
#
# 证据：
#   TableData.CommonData.CurrencyData：crystal -> item_id 101
#   items.json：101 = 龙玉，103 = 九方币
#   CommonConstData 里金币消耗几乎都写成 [103, N]（SigilSynthesisGold、
#   FashionEquipmentSynthesisGold、BattlePassBuyLevelPrice ...），所以 103 是金币。
#
# ItemBag 的 __IntTypeKey__ = false，键是字符串（就是 ItemInfo.id），
# 值类型 ItemInfo 的字段见 config.lua:4357-4396：
#   id(string) / itemId(int) / num(int) / expireTime(double) / getTime(double) / ext(ItemAttrs)
STARTER_ITEMS = {
    "item_bag": [
        {"id": "1", "itemId": 101, "num": 1000},     # 龙玉
        {"id": "2", "itemId": 103, "num": 100000},   # 九方币
    ],
}

# 初始装扮（头像 / 名片）。
#
# 为什么只有这两项：
#   客户端判「能不能选」是 `isUnlock = (data.type == 0 or bag:find(id))`
#   （ui/panel/playerinformationpanel/node_pop_playerinformation_head/
#     node_pop_playerinformation_headgroup.lua:56）。
#   实测各配置表的 type 分布：
#     PlayerPortraitData       164 条  {1:104, 2:60}    <- 没有 type=0
#     PlayerCardData            82 条  {1:69,  2:13}    <- 没有 type=0
#     PlayerPortraitFrameData   23 条  {0:1, 1:5, 2:17} <- 有默认项
#     PlayerChatBubbleData      15 条  {0:1, ...}       <- 有默认项
#     PlayerCardFrameData        1 条  {0:1}            <- 有默认项
#     PlayerTitleData           34 条  {0:2, ...}       <- 有默认项
#     PlayerBackgroundData      30 条  {0:1, ...}       <- 有默认项
#   所以**只有头像和名片**在空 bag 下完全不能选；其余五种有 type=0 兜底，
#   官方默认就是空 bag（generated/config.lua:8911 portrait_bag = {"FashionBag", {}}）。
#
# 为什么是 id 1/2：
#   TableData.CommonData.PlayerPortraitData
#     [1] type=2 name=导演 accessWayDesc="初始头像"
#     [2] type=2 name=导演 accessWayDesc="初始头像"
#   TableData.CommonData.PlayerCardData
#     [1] type=2 name=导演   <- CommonConstData.female_player_card = 1
#     [2] type=2 name=导演   <- CommonConstData.male_player_card   = 2
#
# 注意：player_card_id 依赖 gender，而新号 gender=0（未选），
#   所以**不能在这里定死**，由 game_server/avatar.py::_commit_name_gender
#   在取名拿到 gender 之后写（男=2 女=1）。
STARTER_FASHIONS = {
    "portrait_bag": [1, 2],
    "player_card_bag": [1, 2],
}

# CommonConstData.Default_PlayerHead = 1（默认头像 id）
STARTER_HEAD_ID = 1

_uid = [1000]


def next_id():
    _uid[0] += 1
    return _uid[0]


def extract_block(s, start):
    """从 s[start] 的 '{' 开始配平，返回整块。"""
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return s[start:]


def split_top(inner):
    parts, depth, cur = [], 0, ""
    for c in inner:
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_custom_types(txt):
    """解析 Custom 段：name -> {props: {k: (type, default)}, value_type, int_key}"""
    i = txt.find("  Custom = {")
    if i < 0:
        raise RuntimeError("config.lua 里找不到 Custom 段")
    cblk = extract_block(txt, txt.find("{", i))
    types = {}
    for m in re.finditer(r"^    (\w+) = \{", cblk, re.MULTILINE):
        name = m.group(1)
        blk = extract_block(cblk, cblk.find("{", m.start()))
        props = {}
        pm = blk.find("Properties = {")
        if pm >= 0:
            pb = extract_block(blk, blk.find("{", pm))
            for pm2 in re.finditer(r"^        (\w+) = \{", pb, re.MULTILINE):
                k = pm2.group(1)
                eb = extract_block(pb, pb.find("{", pm2.start()))
                parts = split_top(eb[1:-1])
                typ = parts[0].strip().strip('"') if parts else "?"
                dflt = parts[1].strip() if len(parts) > 1 else None
                props[k] = (typ, dflt)
        vm = re.search(r'__ValueType__\s*=\s*"([^"]+)"', blk)
        im = re.search(r"__IntTypeKey__\s*=\s*(\w+)", blk)
        types[name] = {
            "props": props,
            "value_type": vm.group(1) if vm else None,
            "int_key": bool(im and im.group(1) == "true"),
        }
    return types


def gen_nested(types, typ, depth=0):
    """嵌套值：不带 __tp__，只给 __id__ + 子属性。"""
    out = {"__id__": next_id()}
    info = types.get(typ)
    if info is None or depth >= 4:
        return out
    for k, v in info["props"].items():
        sub = v[0]
        out[k] = DEF[sub] if sub in SIMPLE else gen_nested(types, sub, depth + 1)
    return out


def gen_top(types, typ):
    """顶层值：必须带 __tp__。"""
    out = {"__tp__": typ, "__id__": next_id()}
    info = types.get(typ)
    if info is None:
        return out
    for k, v in info["props"].items():
        sub = v[0]
        out[k] = DEF[sub] if sub in SIMPLE else gen_nested(types, sub, 1)
    return out


def main():
    if not os.path.exists(CONFIG_LUA):
        raise SystemExit(f"找不到 config.lua: {CONFIG_LUA}")
    txt = open(CONFIG_LUA, encoding="utf-8", errors="replace").read()
    types = parse_custom_types(txt)
    print('自定义类型: {}（结构体 {}）'.format(len(types), sum(1 for t in types.values() if t["props"])))

    # AvatarProperties 里的简单类型属性，全部给默认值
    i = txt.find("    AvatarProperties = {")
    blk = extract_block(txt, txt.find("{", i))
    pm = blk.find("Properties = {")
    pb = extract_block(blk, blk.find("{", pm))
    props = {}
    for m in re.finditer(r"^        (\w+) = \{", pb, re.MULTILINE):
        k = m.group(1)
        eb = extract_block(pb, pb.find("{", m.start()))
        parts = split_top(eb[1:-1])
        typ = parts[0].strip().strip('"') if parts else "?"
        if typ in SIMPLE:
            props[k] = DEF[typ]

    # AvatarProperties 里**全部**自定义类型属性，递归展开。
    #
    # 一开始只挑了被 pairs() 遍历的那些，结果非遍历的（如 fishing_miner_act_prop）
    # 照样在组件 start() 里被直接索引而崩。既然客户端要求属性齐全，
    # 就按权威表全给。
    for m in re.finditer(r"^        (\w+) = \{", pb, re.MULTILINE):
        k = m.group(1)
        eb = extract_block(pb, pb.find("{", m.start()))
        parts = split_top(eb[1:-1])
        typ = parts[0].strip().strip('"') if parts else "?"
        if typ not in SIMPLE and k not in props:
            props[k] = gen_top(types, typ)

    # REQUIRED 里有些不在 AvatarProperties 里（item_bag / task_bag / characterBag ...）
    for k, typ in REQUIRED.items():
        if k not in props:
            props[k] = gen_top(types, typ)

    # 简单类型动态根属性（见 SIMPLE_PROPS 注释）
    for k, v in SIMPLE_PROPS.items():
        props.setdefault(k, v)

    # 预置字典条目（见 SEED 注释）
    for k, entries in SEED.items():
        node = props.get(k)
        if not isinstance(node, dict):
            raise SystemExit(f"SEED 目标属性不存在或不是自定义类型: {k}")
        node.update(entries)

    # 初始角色：往 characterBag 里塞一个男主角色
    for prop, chars in STARTER_CHARS.items():
        node = props.get(prop)
        if not isinstance(node, dict):
            raise SystemExit(f"STARTER_CHARS 目标属性不存在: {prop}")
        for char_id, overrides in chars.items():
            info = gen_nested(types, "CharacterInfo")
            info.update(overrides)
            node[f'#{char_id}'] = info

    # ★ 系统解锁 / 任务全完成 等脚手架不在这里生成，
    #   由 game_server/scaffold.py 在运行时按开关覆盖。

    # 初始编队（见 STARTER_FORMATION 注释）
    for prop, items in STARTER_FORMATION.items():
        node = props.get(prop)
        if not isinstance(node, dict):
            raise SystemExit(f"STARTER_FORMATION 目标属性不存在: {prop}")
        for idx, item in enumerate(items, 1):
            ent = gen_nested(types, "Character")
            for k, v in item.items():
                if k == "value":
                    continue
                ent[k] = v
            lst = gen_nested(types, "IntList")
            for i, cv in enumerate(item.get("value") or [], 1):
                lst[f'#{i}'] = cv
            ent["value"] = lst
            node[f'#{idx}'] = ent

    # 初始货币（见 STARTER_ITEMS 注释）
    for prop, entries in STARTER_ITEMS.items():
        node = props.get(prop)
        if not isinstance(node, dict):
            raise SystemExit(f"STARTER_ITEMS 目标属性不存在: {prop}")
        for ent in entries:
            info = gen_nested(types, "ItemInfo")
            info.update(ent)
            node[ent["id"]] = info

    # 初始装扮（见 STARTER_FASHIONS 注释）
    for prop, ids in STARTER_FASHIONS.items():
        node = props.get(prop)
        if not isinstance(node, dict):
            raise SystemExit(f"STARTER_FASHIONS 目标属性不存在: {prop}")
        for fid in ids:
            ent = gen_nested(types, "Fashion")
            ent["id"] = fid
            # FashionBag 的 __IntTypeKey__ = true -> JSON 里用 "#N" 表示整数键
            node[f'#{fid}'] = ent

    # 关键简单值
    props["uid"] = "OO00000001"
    props["username"] = "oologin"
    # 玩家等级：客户端声明默认值。
    # 脚手架要拉高等级请用 --unlock-all（game_server/scaffold.py），不要改这里。
    props["level"] = 1
    # 默认头像：CommonConstData.Default_PlayerHead = 1。
    # 对应 PlayerPortraitData[1].accessWayDesc = "初始头像"。
    # 注意：名片 id（player_card_id）不在这里定，它依赖 gender，
    #   由 avatar.py::_commit_name_gender 在取名后写。
    props["portrait_id"] = STARTER_HEAD_ID
    props["entityType"] = "Avatar"
    # 玩家名/性别：**恢复 generated/config.lua 声明的官方默认值**（不是我们拍的）。
    #
    # 证据：
    #   generated/config.lua:9073-9078
    #     player_name = { "string", "", "OwnClient", "PER" }   <- 官方默认 ""
    #   generated/config.lua:9001-9006
    #     gender = { "int", 0, "OwnClient", "PER" }            <- 官方默认 0
    #
    # 为什么必须是这两个值（客户端判据）：
    #   battle/public/system/nodeskill/viewnode/viewuinode.lua:1402-1416
    #     NamingCheckTemp:onStartFlowIn()
    #       if string.isNilOrEmpty(playerName) or gender == 0 then
    #         Undone   <- 走取名流程（弹 CreatingRolePanel）
    #       else
    #         Done     <- 跳过取名
    #
    # 为什么不能填 "未具名导演"：
    #   CommonConstData.Default_PlayerName = "未具名导演" 是**显示占位符**
    #   （名字为空时界面显示什么），把它写进属性 = 把显示层塞进数据层。
    #   之前写死它 + gender=1，客户端判定「已取名」-> 取名面板永不出现。
    #
    # 为什么不是 nil：
    #   属性声明是 "string"，给 null 会让客户端 initProperty 类型不匹配。
    #
    # 注意：进街区需要 player_name 非空 + gender != 0
    #   （stage/streetstage.lua:172-177 checkIsCanEnterStreet）。
    #   但那是「取名完成后」的状态；新号本来就该先走序章取名。
    #   想跳过序章用 --unlock-all，由 game_server/scaffold.py 补这两个值。
    props["player_name"] = ""
    props["gender"] = 0

    data = {"__Properties__": props}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print(f'属性数: {len(props)}  输出: {OUT} ({os.path.getsize(OUT)} B)')


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
