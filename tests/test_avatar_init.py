#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""initDict（createChannelEntity 的属性表）不变量测试。

用法:
    python tests/test_avatar_init.py

这些断言就是阶段 1 数据模型的"合同"：客户端哪个组件按什么形状读属性，
都记在这里，改生成器时先看这里会不会红。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402

INIT_PATH = os.path.join(ROOT, "game_server", "avatar_init.json")

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    data = json.load(open(INIT_PATH, encoding="utf-8"))
    props = data["__Properties__"]

    print("\n[1] 整体规模")
    check("属性数 212", len(props) == 212, len(props))
    check("每个值都是 dict 或简单类型",
          all(isinstance(v, dict) or isinstance(v, (int, float, str, bool))
              for v in props.values()))

    print("\n[2] 每个自定义类型都带 __tp__ + __id__")
    custom = {k: v for k, v in props.items() if isinstance(v, dict)}
    check("自定义属性都是 dict", len(custom) > 100, len(custom))
    bad = [k for k, v in custom.items()
           if not isinstance(v.get("__tp__"), str) or not isinstance(v.get("__id__"), int)]
    check("__tp__ / __id__ 齐全", not bad, bad[:5])
    ids = [v["__id__"] for v in custom.values()]
    check("__id__ 不重复", len(set(ids)) == len(ids))

    print("\n[3] 1-1 角色数据模型")
    cb = props["characterBag"]
    check("characterBag 是 CharacterPersistentDict",
          cb["__tp__"] == "CharacterPersistentDict", cb["__tp__"])
    check("预置男主角色 9001", "#9001" in cb, [k for k in cb if not k.startswith("__")])
    check("9001 useSkinId = 900101", cb["#9001"]["useSkinId"] == 900101,
          cb["#9001"].get("useSkinId"))
    check("9001 有 CharacterInfo 的字段（skins）", "skins" in cb["#9001"])

    print("\n[4] 1-2 背包与货币")
    ib = props["item_bag"]
    check("item_bag 是 ItemBag", ib["__tp__"] == "ItemBag", ib["__tp__"])
    entries = {k: v for k, v in ib.items() if not k.startswith("__")}
    check("有 2 个条目", len(entries) == 2, len(entries))
    by_item = {v["itemId"]: v for v in entries.values()}
    check("龙玉(item 101) 1000", by_item.get(101, {}).get("num") == 1000)
    check("九方币(item 103) 100000", by_item.get(103, {}).get("num") == 100000)
    check("每个条目都有 id/num", all("id" in v and "num" in v for v in entries.values()))

    print("\n[5] 1-3 玩家数据")
    check("player_name 非空", isinstance(props.get("player_name"), str)
          and props["player_name"] != "", props.get("player_name"))
    check("gender = 1（男）", props.get("gender") == 1, props.get("gender"))
    check("story_finished[1] 为真", props["story_finished"].get("#1") is True)
    # ★ street_id 必须是 101，不能是 0。
    # loginmanager.lua:118-135：street_id ~= 0 走 JumpToStreetStage，
    # 否则走 JumpToHallStage（大厅 sc_mainstage，这一版资源里没有）。
    # CommonConstData.DefaultStreet = 101，StreetData[101].scene = sc_zhpmap_01_streaming。
    check("street_id = 101（走街区而不是大厅）", props.get("street_id") == 101, props.get("street_id"))

    print("\n[5b] 编队（决定街道里生成哪个玩家角色）")
    # avatarformationcomp.lua:32-37 getFormationInfo(index) 取 self.character_list[index]，
    # 兜底是 {value = {}, name = ""}，也就是 Custom.Character 的两个字段。
    # 编队为空 -> 街道系统没有出战角色 -> 不生成玩家 Actor -> 摄像机没有跟随目标。
    cl = props.get("character_list")
    check("character_list 是 CharacterList", isinstance(cl, dict) and cl.get("__tp__") == "CharacterList",
          (cl or {}).get("__tp__"))
    check("有第 1 队", isinstance(cl, dict) and "#1" in cl)
    team = (cl or {}).get("#1") or {}
    check("第 1 队有 value 列表", isinstance(team.get("value"), dict))
    check("第 1 队出战角色是 9001", team.get("value", {}).get("#1") == 9001, team.get("value"))
    # ★ 编队是 3 个格子，空位必须是 0，不能缺。
    # streetv2panel.lua:1324-1331 initFormation:
    #   for i = 1, 3 do local charId = formation.value[i] ...
    #   if member and 0 < charId then   <- charId 为 nil 时抛 compare number with nil
    # 只给 1 个元素 -> i=2 是 nil -> initEx 被打断 -> 面板建了又关 -> 零 HUD。
    v = team.get("value", {})
    check("编队第 2 格是 0", v.get("#2") == 0, v.get("#2"))
    check("编队第 3 格是 0", v.get("#3") == 0, v.get("#3"))
    check("choosen_index = 1", props.get("choosen_index") == 1, props.get("choosen_index"))
    # streetplayerrolemgr.lua:17 -> return GlobalData.Avatar.character_choosen_char
    # loadstepstreet.lua:129 -> if data[curCharId] then 才会 loadActorView 加载玩家模型
    # 给 0 的话 data[0] 是 nil，直接跳过模型加载：场景在、人不在。
    check("character_choosen_char = 9001", props.get("character_choosen_char") == 9001,
          props.get("character_choosen_char"))

    print("\n[5c] avatar_init.json 里**不应**含脚手架")
    # ★ 架构决定：系统解锁 / 任务全完成 / 等级拉高 这些脚手架是**运行时可开关的
    #   覆盖层**（game_server/scaffold.py），不再烤进 avatar_init.json。
    #   原因：烤进去之后就关不掉，而且会掩盖服务端真正的 bug。
    #   脚手架本身的验证在 tests/test_scaffold.py。
    sysp = props.get("system_p")
    check("system_p 存在", isinstance(sysp, dict), type(sysp))
    us = (sysp or {}).get("unlocked_system") or {}
    check("unlocked_system 是空的（脚手架默认关）",
          len([k for k in us if k.startswith("#")]) == 0,
          len([k for k in us if k.startswith("#")]))
    check("level = 1（客户端声明默认值）", props.get("level") == 1, props.get("level"))
    check("done_tasks 是空的",
          len([k for k in (props.get("done_tasks") or {}) if k.startswith("#")]) == 0)
    check("done_story_tasks 是空的",
          len([k for k in (props.get("done_story_tasks") or {}) if k.startswith("#")]) == 0)
    check("task_bag 是空的",
          len([k for k in (props.get("task_bag") or {}) if k.startswith("#")]) == 0)

    print("\n[6] 整数键约定（CustomDict:roinit 不做键类型转换）")
    conv = msmod._conv_int_keys({"a": {"#1": True, "#12": 3, "b": {"#7": 0}}})
    check("#1 -> 1", 1 in conv["a"])
    check("#12 -> 12", 12 in conv["a"])
    check("嵌套也转换", 7 in conv["a"]["b"])
    check("普通键保留", "b" in conv["a"])

    print("\n[7] 整体 msgpack 编码可解码且能还原 story_finished[1]")
    raw = msmod.mp_map(msmod._conv_int_keys(props))
    check("编码非空", len(raw) > 10000, len(raw))
    back, used = msmod.mp_decode(raw)
    check("解码消耗完整个缓冲区", used == len(raw), (used, len(raw)))
    check("解码后 story_finished 有整数键 1", 1 in back["story_finished"])
    check("解码后 characterBag 有整数键 9001", 9001 in back["characterBag"])

    print("\n[8] 阶段 1 三个组件的属性覆盖（config.lua PropertyCallbacks 反查）")
    # 这三个列表由 work/oo_analysis/check_p1_coverage.py 从
    # generated/config.lua 的 PropertyCallbacks 里抽出来：
    # 回调的 keyPath 去掉 .xxx 之后就是属性根名。客户端在对应 _SC 回调里
    # 会直接 index 这些属性，缺一个就是 nil 崩，所以阶段 1 的验收标准是
    # 「一个都不能少」。
    p1_roots = {
        "AvatarCharacterComp": ["characterBag"],
        "AvatarBagComp": ["item_bag"],
        "AvatarUserDataComp": [
            "background_bag", "background_id", "canScore", "chat_bubble_bag",
            "gender", "hasScore", "is_account_create_time_hidden",
            "player_card_bag", "player_title_bag", "portrait_bag",
            "portrait_frame_bag", "portrait_frame_id", "portrait_id",
            "shown_titles", "signature",
        ],
    }
    for comp, roots in p1_roots.items():
        for r in roots:
            check(comp + " 需要 " + r, r in props, "initDict 里没有")

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
