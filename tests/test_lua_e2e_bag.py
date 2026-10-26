#!/usr/bin/env python
"""背包/道具数据的同源 Lua E2E 测试。

## 这是什么

用 lupa 内嵌的 **Lua 5.3**（与游戏 xLua 同版本）加载 archive 里 decompile 出来的
**同一份客户端 Lua**，把服务端造出来的数据结构喂给客户端自己的
CustomTypeFactory -> CustomDict:roinit 解析。

不是 mock：跑的是客户端解析逻辑本体。2026-10-26 两次「整号登不上」正是
服务端数据结构缺 __id__、客户端这里断言失败造成的，服务端日志完全正常。
这个测试把那类事故变成「一条命令就能发现」。

## 判定

  - 合法结构必须解析成功，且字段读得对
  - 缺 __id__ / 类型不对必须被客户端拒绝（证明测试有牙）

用法: python tests/test_lua_e2e_bag.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_and_read, parse_as_client  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


ITEM_OK = {
    "__id__": 9001, "id": "g1", "itemId": 101, "num": 7,
    "getTime": 0.0, "expireTime": 0.0,
    "ext": {"__id__": 9002},
}


def run():
    if not os.path.isdir(LUA_ROOT):
        print("跳过：找不到客户端 Lua 目录 " + LUA_ROOT)
        return 0

    print("\n[1] 客户端 Lua 能启动（Lua 5.3 + 全量自定义类型）")
    lua = new_runtime()
    check("运行时是 Lua 5.3", "5.3" in lua.eval("_VERSION"), lua.eval("_VERSION"))
    boot_config(lua)
    n_custom = lua.eval("(function() local n=0 for _ in pairs(Config.Custom) do n=n+1 end return n end)()")
    check("config.Custom 类型数量 > 300", int(n_custom) > 300, n_custom)
    check("ItemInfo 已注册", lua.eval('require("Core.PropertySync.CustomTypeFactory").hasRegistered("ItemInfo")') is True)

    print("\n[2] 合法 ItemInfo：客户端能解析，字段读得对")
    ok, err, got = parse_and_read(lua, "ItemInfo", ITEM_OK, ("itemId", "num"))
    check("解析成功", ok, err)
    check("itemId 读得对", ok and int(got["itemId"]) == 101, got)
    check("num 读得对", ok and int(got["num"]) == 7, got)

    print("\n[3] 缺 __id__ 的 ext：客户端必须拒绝（2026-10-26 事故类）")
    bad = dict(ITEM_OK)
    bad["ext"] = {}
    ok2, err2 = parse_as_client(lua, "ItemInfo", bad)
    check("被客户端断言拦下", not ok2, err2[:120])
    check("错误信息指向 CustomDict", "CustomDict.lua" in err2, err2[:160])

    print("\n[4] 顶层缺 __id__：同样必须拒绝")
    bad2 = dict(ITEM_OK)
    bad2.pop("__id__")
    ok3, err3 = parse_as_client(lua, "ItemInfo", bad2)
    check("被客户端断言拦下", not ok3, err3[:120])

    print("\n[5] 装饰 Fashion：{__id__, id, expiration_time} 能被解析")
    fashion = {"__id__": 9100, "id": 513, "expiration_time": 0}
    ok4, err4, got4 = parse_and_read(lua, "Fashion", fashion, ("id", "expiration_time"))
    check("Fashion 解析成功", ok4, err4)
    check("id 读得对", ok4 and int(got4["id"]) == 513, got4)

    print("\n[6] 短信三层结构（回归：曾经整号登不上）")
    npc = {
        "__id__": 1128,
        "address_list": {
            "__id__": 1129,
            100103: {
                "__id__": 2887,
                1014: {
                    "__id__": 2959, "chat_type": 1, "npc_id": 100103,
                    "group_npc_ids": {"__id__": 2960},
                    "topic_infos": {"__id__": 2961},
                    "is_over": 0, "update_time": 1700000000,
                },
            },
        },
        "resident_topic_ids": {"__id__": 1134},
    }
    ok5, err5 = parse_as_client(lua, "NpcChatProp", npc)
    check("NpcChatProp 完整结构解析成功", ok5, err5)

    print("\n[7] 故意抽掉内层 __id__：必须被拒绝（证明检查没有放水）")
    import copy
    npc_bad = copy.deepcopy(npc)
    npc_bad["address_list"][100103][1014].pop("topic_infos")
    npc_bad["address_list"][100103][1014]["topic_infos"] = {}
    ok6, err6 = parse_as_client(lua, "NpcChatProp", npc_bad)
    check("缺 topic_infos.__id__ 被拒绝", not ok6, err6[:120])

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
