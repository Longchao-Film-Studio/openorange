#!/usr/bin/env python
"""属性树结构校验（game_server/property/validate.py）测试。

为什么单独测：这条不变量是「整号登不上」事故的通用防线 ——
客户端 CustomDict/CustomList 无条件 assert(__id__ ~= nil)，
服务端 Tx 只给顶层对象补 id。校验必须**对所有自定义类型生效**，
不能只认短信那一个。所以这里的用例故意用背包、街景等其他系统构造坏数据。

用法: python tests/test_property_validate.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_validate_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server.property import (  # noqa: E402
    ids,
    spec,  # noqa: E402
    validate,
)
from game_server.property.tx import Tx, TxError  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[0] 前提：声明表覆盖了别的系统（不是只有短信）")
    check("item_bag 根属性有声明", spec.get("item_bag") is not None)
    check("ItemBag 是容器且元素是 ItemInfo",
          spec.value_type("ItemBag") == "ItemInfo", spec.value_type("ItemBag"))
    check("ItemInfo 有嵌套自定义字段 ext(ItemAttrs)",
          "ext" in spec.struct_fields("ItemInfo"), spec.struct_fields("ItemInfo"))
    check("street_info 根属性有声明", spec.get("street_info") is not None)

    print("\n[1] 背包：ItemInfo.ext 缺 __id__ 必须被抓到（证明不是短信专用）")
    props = {"item_bag": {
        "__id__": 100,
        5001: {"__id__": 101, "itemId": 5001, "num": 1, "ext": {"atk": 1}},
    }}
    miss = validate.find_missing_ids(props)
    check("抓到 item_bag[5001].ext",
          any(p == "item_bag[5001].ext" and t == "ItemAttrs" for p, t in miss), miss)
    check("有 id 的 ItemInfo 本身不误报", not any(t == "ItemInfo" for _, t in miss), miss)

    print("\n[2] 街景：容器节点缺 __id__ 必须被抓到")
    props2 = {"street_info": {
        "__id__": 200,
        101: {"__id__": 201, "exploration_info": {}},
    }}
    miss2 = validate.find_missing_ids(props2)
    check("抓到 street_info[101].exploration_info",
          any(p == "street_info[101].exploration_info" for p, _ in miss2), miss2)
    check("StreetInfo 本身有 id 不误报", not any(t == "StreetInfo" for _, t in miss2), miss2)

    print("\n[3] 元字段 / 服务端私有字段 / 混合类型自身字段：不误报")
    props3 = {
        "item_bag": {"__id__": 100, "_oo_next": 7,
                     5001: {"__id__": 101, "itemId": 5001,
                            "ext": {"__id__": 102}}},
        "npc_chat_prop": {
            "__tp__": "NpcChatProp", "__id__": 5,
            "address_list": {"__id__": 6, 100103: {
                "__id__": 7,
                1014: {"__id__": 8, "is_over": 0, "update_time": 1,
                       "group_npc_ids": {"__id__": 9}, "topic_infos": {"__id__": 10}},
            }},
            "resident_topic_ids": {"__id__": 11},
        },
    }
    check("整棵树干净 -> 0 违规", validate.find_missing_ids(props3) == [],
          validate.find_missing_ids(props3))
    check("不变量成立", validate.invariant_holds(props3) is True)

    print("\n[4] 读档路径：ids.reconcile 遇到缺 id 不抛异常（只告警）")
    bad = {"item_bag": {"__id__": 100, 5002: {"itemId": 5002}}}
    err = None
    res = None
    try:
        res = ids.reconcile(bad, warn=False)
    except Exception as e:                      # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    check("reconcile 不抛异常", err is None, err)
    check("reconcile 返回 (fixed, dup) 两元组",
          isinstance(res, tuple) and len(res) == 2, res)
    check("reconcile 之后缺 id 依然能被找到",
          validate.find_missing_ids(bad) != [], validate.find_missing_ids(bad))

    print("\n[5] 写入路径：Tx.set 嵌套缺 __id__ -> 直接报错（不等玩家登录）")
    props5 = {"item_bag": {"__id__": 100}}
    tx = Tx(props5)
    try:
        tx.set(("item_bag", 5003), {"__id__": 300, "itemId": 5003,
                                    "ext": {"atk": 1}})
        check("缺 id 必须抛 TxError", False, "没有抛")
    except TxError as e:
        check("缺 id 必须抛 TxError", "缺 __id__" in str(e), str(e)[:80])
    check("抛错后 props 没有被写脏", 5003 not in props5["item_bag"],
          props5["item_bag"].keys())

    print("\n[6] 写入路径：id 齐全则正常通过")
    tx2 = Tx(props5)
    out = tx2.set(("item_bag", 5004), {"__id__": 301, "itemId": 5004,
                                       "ext": {"__id__": 302}})
    check("带 id 可写", out.get("__id__") == 301, out)
    check("写后树仍然干净", validate.find_missing_ids(props5) == [],
          validate.find_missing_ids(props5))
    check("有帧产生", len(tx2.frames()) > 0, len(tx2.frames()))

    print("\n[7] 写入路径：顶层对象缺 id 时 Tx 自动补（这是既有契约，不能破）")
    tx3 = Tx({"item_bag": {"__id__": 100}})
    got = tx3.set(("item_bag", 5005), {"itemId": 5005})
    check("顶层自动分配 __id__", isinstance(got.get("__id__"), int) and got["__id__"] > 0, got)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
