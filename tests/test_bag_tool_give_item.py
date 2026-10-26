#!/usr/bin/env python
"""离线发道具工具（tools/give_item.py）与背包唯一入口的一致性测试。

工具以前自己拼格子（max_id + 自己算键、没有 ext/maxNum/autoUse），
现在必须走 grants；下面这些断言在旧实现下会直接红：

  · 键必须是规范键 g<n>（旧实现是 "1"/"2"）
  · 超过 maxNum 必须拆格（旧实现永远塞一格）
  · notStack 必须每份一个格子
  · autoUse 发放即消耗、不该留格子
  · 未知道具必须失败而不是硬造

用法: python tests/test_bag_tool_give_item.py
"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_give_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.property.bag import keys  # noqa: E402
from tools.give_item import give  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh():
    return copy.deepcopy(initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"]))


def pick(pred):
    for it in gamedata.table("ItemData").values():
        if isinstance(it, dict) and it.get("itemId") and pred(it):
            return it
    return None


def grids_of(props, item_id):
    return [g for g in props["item_bag"].values()
            if isinstance(g, dict) and int(g.get("itemId") or 0) == int(item_id)]


def run():
    print("\n[1] 规范键 + maxNum 拆格")
    it = pick(lambda x: not x.get("autoUse") and not x.get("notStack")
              and int(x.get("maxNum") or 0) == 1000)
    check("找到 maxNum=1000 且非 autoUse/notStack 的道具", it is not None)
    if it:
        props = fresh()
        key, total, how = give(props, it["itemId"], 1500)
        gs = grids_of(props, it["itemId"])
        check("拆成 2 格", len(gs) == 2, len(gs))
        check("总数 1500", sum(int(g["num"]) for g in gs) == 1500, [g["num"] for g in gs])
        check("键是规范键 g<n>", keys.is_canonical(key), key)
        check("说明是「新格子」", how == "新格子", how)
        check("每格都带 __id__/ext.__id__",
              all(isinstance(g.get("__id__"), int) and isinstance((g.get("ext") or {}).get("__id__"), int)
                  for g in gs), gs[:1])

    print("\n[2] notStack 每份一格")
    ns = pick(lambda x: x.get("notStack") and not x.get("autoUse"))
    check("找到 notStack 道具", ns is not None)
    if ns:
        props = fresh()
        give(props, ns["itemId"], 3)
        gs = grids_of(props, ns["itemId"])
        check("3 份 -> 3 格", len(gs) == 3, len(gs))
        check("每格数量 1", all(int(g["num"]) == 1 for g in gs), [g["num"] for g in gs])

    print("\n[3] autoUse 道具发放即消耗，不留格子")
    au = pick(lambda x: x.get("autoUse"))
    check("找到 autoUse 道具", au is not None)
    if au:
        props = fresh()
        key, total, how = give(props, au["itemId"], 1)
        check("没有格子", not grids_of(props, au["itemId"]) and key == "", key)
        check("说明是「自动使用」", how == "自动使用", how)

    print("\n[4] 未知道具 id 必须失败（不许硬造）")
    props = fresh()
    err = None
    try:
        give(props, 9999999, 1)
    except RuntimeError as e:
        err = e
    check("抛 RuntimeError", err is not None, err)
    check("包里没有被造出假道具",
          not any(isinstance(g, dict) and int(g.get("itemId") or 0) == 9999999
                  for g in props["item_bag"].values()))

    print("\n[5] 叠加：已有格子时走 OP_CHANGE 语义（不再新建）")
    st = pick(lambda x: not x.get("autoUse") and not x.get("notStack")
              and int(x.get("maxNum") or 0) >= 100)
    if st:
        props = fresh()
        give(props, st["itemId"], 1)
        n1 = len(grids_of(props, st["itemId"]))
        _k, total, how = give(props, st["itemId"], 2)
        check("仍是同一批格子（没新建）", len(grids_of(props, st["itemId"])) == n1, len(grids_of(props, st["itemId"])))
        check("总数累加", total == 3, total)
        check("说明是「叠加」", how == "叠加", how)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
