#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

# 需要预置的属性 -> 类型。
# 每一条的来源都写在注释里：要么来自 AvatarProperties 的权威声明，
# 要么来自某个组件在 init/start/onBecomePlayer 里 pairs() 它而崩掉的实机日志。
REQUIRED = {
    # --- AvatarBagComp:refreshLocalBagData -> pairs(self.item_bag) ---
    "item_bag": "ItemBag",
    # --- AvatarTaskComp:initDoneAchievement / getAllDoingDailyTaskItem -> task_bag ---
    "task_bag": "TaskBag",
    # --- AvatarActivityComp -> pairs(cur_activity_info) ---
    "cur_activity_info": "ActivityProp",
    # --- AvatarAchieveComp:initDoneAchievement -> pairs(achieve_type_bag) ---
    "achieve_type_bag": "AchieveTypeBag",
    # --- AvatarCharacterComp -> pairs(characterBag) / characterBag[charId].xxx ---
    # ★ 2026-10-19 修正：原来写成 CharacterList 是错的。
    #   CharacterList 继承 CustomList（列表），而 avatarcharactercomp 是这么用的：
    #     :79   self.characterBag[charID].biography_read
    #     :533  self.characterBag[charId].breakAward
    #     :811  return self.characterBag[charId].useSkinId
    #     :822  self.characterBag[charId].skins[charAppearanceId]
    #   这些都是 CharacterInfo 的字段；列表没法按 charId 索引。
    #   config.lua 里 CharacterPersistentDict 才是 __ValueType__=CharacterInfo、
    #   __IntTypeKey__=true 的字典，而且
    #   gamecommon/customtypes/character/characterpersistentdict.lua 上就写着
    #   HasChar(no) / GetAllSkins / CheckAddSkin，正是 characterBag 的用法。
    "characterBag": "CharacterPersistentDict",
    # --- AvatarStreetComp -> pairs(street_info) / pairs(street_dispatch) ---
    "street_info": "StreetInfoDict",
    "street_dispatch": "StreetDispatchDict",
    # --- AvatarStorylineComp -> self.story_finished[id] or false ---
    "story_finished": "BooleanDictKeyInt",
    "story_max_collect_num": "IntDictKeyInt",
    "story_reward_list": "IntListDictKeyInt",
    # --- AvatarSystemGuideComp -> pairs(system_guide_records) ---
    "system_guide_records": "IntDictKeyInt",
    "battle_teach_state": "IntDictKeyInt",
    # --- PartyGameDataModel:15 -> self.partygame_p ---
    "partygame_p": "PartyGameProp",
    # --- 其余来自 AvatarProperties 的遍历型属性 ---
    "accu_charge_gacha_p": "AccuChargeGachaSetProp",
    "bp_info": "BpInfo",
    "card_prop": "CardDictProp",
    "championship_data": "ChampionshipData",
    "chat_p": "ChatProp",
    "contribution_player_cycle": "ContributionPlayerInfoRecord",
    "gacha_prop": "GachaProp",
    "multitower_task_prop": "ActiveTaskBag",
    "popup_gift_p": "PopupGiftProp",
    "returning_player_act_p": "ReturningPlayerActProp",
    "rogue_p": "RogueProp",
    "street_info_2d": "StreetInfoDict2D",
    "weekly_play_bp_p": "WeeklyPlayBPProp",
    "soul_task_bag": "SoulTaskBag",
    "story_task_bag": "StoryTaskBag",
    "done_tasks": "DoneTaskTypeBag",
    "done_story_tasks": "StoryTaskBag",
    # --- 动态根属性：只出现在 PropertyCallbacks、AvatarProperties 里没有声明 ---
    # 证据：generated/config.lua:6370 的 Custom.SingleMatchInfo 定义了
    #   state(int) / match_id(string) / match_info(MatchInfo)；
    #   entities/avatarcomponent/avatarteamcomp.lua:648 不判空直接
    #   self.singleMatchInfo.state —— 空指针就会把 HallStage 进场打断。
    # 真实服务端是用 _changeRootProperty 运行时建这个属性（带 __tp__），
    # 我们改从 initDict 建，等价。
    "singleMatchInfo": "SingleMatchInfo",
}

# PropertyCallbacks 里出现、但 AvatarProperties 没有声明的"简单类型"动态根属性。
# 表来自 work/oo_analysis/missing_props.py：它把各 Avatar*Comp 的
# PropertyCallbacks 段和 AvatarProperties 对差，并按回调里声明的类型
# （number/string/boolean）分类。客户端访问这些属性时基本不判空，
# 所以 initDict 里必须先给默认值。
SIMPLE_PROPS = {
    "avatarNowState": 0,
    "avatarTeamMatchState": 0,
    "battle_prop_choosen_index": 0,
    "character_choosen_char": 0,
    "choosen_index": 0,
    "daily_active_value": 0,
    "endless_choosen_index": 0,
    "four_racing_character": 0,
    "has_seen_unlock_action": False,
    "is_rank_mode_available": False,
    "mvp_count": 0,
    "normal_match_count": 0,
    "normal_mode_win_count": 0,
    "normal_mvp_count": 0,
    "punishment_end_time": 0,
    "puzzle_character": 0,
    "rank_match_count": 0,
    "rank_mode_win_count": 0,
    "rank_mvp_count": 0,
    "rank_score": 0,
    "street_id": 0,
    "street_trace_taskid": 0,
    "temp_floor": 0,
    "winNum": 0,
}

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
    # 序章要 Cmd_Avatar_StartGame_CS 返回 spaceInfo 才能进场景，
    # 那是完整的匹配/进场流程。把 1 号剧情标记为已完成，
    # 客户端就直接走 LoginSuccess -> JumpToHallStage，等价于"老玩家回流"。
    "story_finished": {"#1": True},
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
# 所以给一个男主角色（gender=1 走 male_player_char=9001）。
STARTER_CHARS = {
    "characterBag": {9001: {"useSkinId": 900101}},
}

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
    for m in re.finditer(r"^    (\w+) = \{", cblk, re.M):
        name = m.group(1)
        blk = extract_block(cblk, cblk.find("{", m.start()))
        props = {}
        pm = blk.find("Properties = {")
        if pm >= 0:
            pb = extract_block(blk, blk.find("{", pm))
            for pm2 in re.finditer(r"^        (\w+) = \{", pb, re.M):
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
        raise SystemExit("找不到 config.lua: %s" % CONFIG_LUA)
    txt = open(CONFIG_LUA, encoding="utf-8", errors="replace").read()
    types = parse_custom_types(txt)
    print("自定义类型: %d（结构体 %d）"
          % (len(types), sum(1 for t in types.values() if t["props"])))

    # AvatarProperties 里的简单类型属性，全部给默认值
    i = txt.find("    AvatarProperties = {")
    blk = extract_block(txt, txt.find("{", i))
    pm = blk.find("Properties = {")
    pb = extract_block(blk, blk.find("{", pm))
    props = {}
    for m in re.finditer(r"^        (\w+) = \{", pb, re.M):
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
    for m in re.finditer(r"^        (\w+) = \{", pb, re.M):
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
            raise SystemExit("SEED 目标属性不存在或不是自定义类型: %s" % k)
        node.update(entries)

    # 初始角色：往 characterBag 里塞一个男主角色
    for prop, chars in STARTER_CHARS.items():
        node = props.get(prop)
        if not isinstance(node, dict):
            raise SystemExit("STARTER_CHARS 目标属性不存在: %s" % prop)
        for char_id, overrides in chars.items():
            info = gen_nested(types, "CharacterInfo")
            info.update(overrides)
            node["#%d" % char_id] = info

    # 关键简单值
    props["uid"] = "OO00000001"
    props["username"] = "oologin"
    props["level"] = 1
    props["entityType"] = "Avatar"
    # 玩家名/性别：streetstage.lua:172-177 的 checkIsCanEnterStreet 要求
    # player_name 非空、gender 非 0。名字取 CommonConstData.Default_PlayerName，
    # gender=1 是男性（loginmanager.lua:142-148 switchVoice 用 1=男 2=女）。
    props["player_name"] = "未具名导演"
    props["gender"] = 1

    data = {"__Properties__": props}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print("属性数: %d  输出: %s (%d B)"
          % (len(props), OUT, os.path.getsize(OUT)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
