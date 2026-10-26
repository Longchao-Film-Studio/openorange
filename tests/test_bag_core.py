#!/usr/bin/env python
"""背包内核（keys / grants / item_use）不变量测试。

覆盖 docs/方案-背包与道具效果.md §6 的 INV-ITEM-1..4 与结构正确性。
数据驱动：不手写道具 id，从 ItemData 里按字段挑（新增道具自动纳入范围）。

用法: python tests/test_bag_core.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_bag_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.property import bag  # noqa: E402
from game_server.property.bag import grants, item_use, keys  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid="bagtest"):
    props = initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"])
    import copy
    props = copy.deepcopy(props)
    return pstate.PlayerState(uid=uid, props=props)


def all_items():
    table = gamedata.table("ItemData")
    return [v for v in table.values() if isinstance(v, dict) and "itemId" in v]


def pick(pred, what):
    for it in all_items():
        if pred(it):
            return it
    raise SystemExit("找不到符合条件的道具：" + what)


def run():
    items = all_items()
    check("ItemData 可读", len(items) > 2000, len(items))

    # ---- 数据驱动的样本 ----
    stackable = pick(lambda it: not it.get("notStack") and not it.get("autoUse")
                     and int(it.get("maxNum") or 0) >= 5, "可叠加且 maxNum>=5 且非 autoUse")
    not_stack = pick(lambda it: it.get("notStack") and not it.get("autoUse"),
                     "notStack 唯一实例且非 autoUse")
    deco = pick(lambda it: it.get("useType") == "avatarAttr"
                and "Portrait" in ((it.get("useParam") or {}).get("KeyList") or []), "装饰类道具")
    counter = pick(lambda it: it.get("useType") == "avatarAttr"
                   and "Exp" in ((it.get("useParam") or {}).get("KeyList") or []), "Exp 计数道具")
    unsupported = pick(lambda it: it.get("useType") in bag.effects.UNSUPPORTED_USE_TYPES, "暂不支持的 useType")
    print(f"  样本：stackable={stackable['itemId']} notStack={not_stack['itemId']} "
          f"deco={deco['itemId']} counter={counter['itemId']} unsupported={unsupported['itemId']}")

    print("\n[1] 钥匙规范：单调递增、删除后不复用")
    st = fresh("keys1")
    k1 = keys.alloc(st.props)
    k2 = keys.alloc(st.props)
    check("钥匙形如 g<n>", k1.startswith("g") and k1[1:].isdigit(), k1)
    check("严格递增", int(k2[1:]) == int(k1[1:]) + 1, (k1, k2))
    st.props["item_bag"][k1] = {"__id__": 1, "id": k1, "itemId": 1, "num": 1}
    del st.props["item_bag"][k1]
    k3 = keys.alloc(st.props)
    check("删除后不复用", k3 != k1 and int(k3[1:]) == int(k2[1:]) + 1, (k1, k3))

    print("\n[2] 旧存档迁移：旧键 -> g<n>，id 同步")
    st2 = fresh("keys2")
    st2.props["item_bag"]["1"] = {"__id__": 11, "id": "1", "itemId": 101, "num": 2}
    st2.props["item_bag"]["2"] = {"__id__": 12, "id": "2", "itemId": 102, "num": 3}
    n = keys.migrate(st2.props)
    check("迁移了 2 个", n == 2, n)
    check("旧键消失", "1" not in st2.props["item_bag"] and "2" not in st2.props["item_bag"])
    new_keys = [k for k in st2.props["item_bag"] if not str(k).startswith("__")]
    check("新键都是规范形式", all(keys.is_canonical(k) for k in new_keys), new_keys)
    check("id 与键一致", all(st2.props["item_bag"][k]["id"] == k for k in new_keys))
    check("幂等：再迁移一次为 0", keys.migrate(st2.props) == 0)

    print("\n[3] 发放：叠加 / 唯一实例 / maxNum")
    st3 = fresh("grant1")
    tx = Tx(st3.props)
    o1 = grants.grant(st3.props, int(stackable["itemId"]), 3, tx=tx, source="test")
    check("首发成功", o1.ok and o1.new_grids == 1, o1)
    tx2 = Tx(st3.props)
    o2 = grants.grant(st3.props, int(stackable["itemId"]), 2, tx=tx2, source="test")
    check("二次发放合并到同一格", o2.ok and o2.new_grids == 0, o2)
    grids = [g for g in st3.props["item_bag"].values()
             if isinstance(g, dict) and int(g.get("itemId") or 0) == int(stackable["itemId"])]
    check("只有 1 个格子", len(grids) == 1, len(grids))
    check("数量正确 3+2=5", int(grids[0]["num"]) == 5, grids[0].get("num"))

    st4 = fresh("grant2")
    for _ in range(2):
        grants.grant(st4.props, int(not_stack["itemId"]), 1, tx=Tx(st4.props), source="test")
    gs = [g for g in st4.props["item_bag"].values()
          if isinstance(g, dict) and int(g.get("itemId") or 0) == int(not_stack["itemId"])]
    check("notStack 开两格", len(gs) == 2, len(gs))

    # ★ 不变量：notStack 且**没配 maxNum** 时，每份仍必须独占一格（num=1）。
    #   反例：之前 notStack 走「没配 maxNum = 不限」的路径，3 份塞进 1 格 ->
    #   客户端按格子当独立实例（附魔/强化挂格子），实例语义就乱了。
    _ns2 = next((it for it in items if it.get("notStack") and not it.get("autoUse")
                 and not it.get("maxNum")), None)
    if _ns2:
        st4b = fresh("bag4b")
        for _ in range(3):
            grants.grant(st4b.props, int(_ns2["itemId"]), 1, tx=Tx(st4b.props), source="test")
        _gs = [g for g in st4b.props["item_bag"].values()
               if isinstance(g, dict) and int(g.get("itemId") or 0) == int(_ns2["itemId"])]
        check("notStack 无 maxNum：3 份仍是 3 格", len(_gs) == 3, len(_gs))
        check("notStack 无 maxNum：每格 num=1", all(int(g["num"]) == 1 for g in _gs),
              [g["num"] for g in _gs])
    check("两格 __id__ 不同", gs[0]["__id__"] != gs[1]["__id__"])
    check("两格 id 与键一致",
          all(st4.props["item_bag"][g["id"]] is g for g in gs) if gs else False)

    print("\n[4] 发放：结构合法（ext 容器带 __id__、过期字段齐全）")
    check("ext 存在且带 __id__", isinstance(grids[0].get("ext"), dict) and "__id__" in grids[0]["ext"], grids[0].get("ext"))
    for f in ("id", "itemId", "num", "getTime", "expireTime"):
        check(f"  格子含 {f}", f in grids[0])

    print("\n[5] 使用：暂不支持的效果 -> 不扣道具 + 15009（INV-ITEM-1）")
    st5 = fresh("use1")
    grants.grant(st5.props, int(unsupported["itemId"]), 1, tx=Tx(st5.props), source="test")
    before = sum(int(g.get("num") or 0) for g in st5.props["item_bag"].values()
                 if isinstance(g, dict) and int(g.get("itemId") or 0) == int(unsupported["itemId"]))
    frames, err, _tail = item_use.apply(st5, int(unsupported["itemId"]), 1, {})
    after = sum(int(g.get("num") or 0) for g in st5.props["item_bag"].values()
                if isinstance(g, dict) and int(g.get("itemId") or 0) == int(unsupported["itemId"]))
    check("返回错误码 15009", err is not None and err.code == 15009, err)
    check("道具数量不变", before == after == 1, (before, after))
    check("没有帧", frames == [])

    print("\n[6] 发放装饰道具（autoUse）-> 直接进 portrait_bag，不留格子")
    st6 = fresh("use2")
    deco_id = int((deco.get("useParam") or {}).get("Portrait"))
    out6 = grants.grant(st6.props, int(deco["itemId"]), 1, tx=Tx(st6.props), source="test")
    check("发放成功", out6.ok, out6)
    got = st6.props.get("portrait_bag", {}).get(deco_id)
    check("portrait_bag 有该条目", isinstance(got, dict), st6.props.get("portrait_bag"))
    check("条目 id 正确", isinstance(got, dict) and int(got.get("id") or 0) == deco_id, got)
    check("autoUse 直接生效（auto_used=1）", out6.auto_used == 1, out6)
    check("没有留下格子",
          not any(isinstance(g, dict) and int(g.get("itemId") or 0) == int(deco["itemId"])
                  for g in st6.props["item_bag"].values()))

    print("\n[7] 发放计数道具（autoUse）-> 属性增加")
    st7 = fresh("use3")
    exp_before = int(st7.props.get("exp") or 0)
    out7 = grants.grant(st7.props, int(counter["itemId"]), 2, tx=Tx(st7.props), source="test")
    check("发放成功", out7.ok, out7)
    check("exp 增加了 2", int(st7.props.get("exp") or 0) == exp_before + 2,
          (exp_before, st7.props.get("exp")))

    print("\n[8] selectItem 缺 ext.itemId -> 不消耗（INV-ITEM-5）")
    sel = next((it for it in items if it.get("useType") == "selectItem"), None)
    if sel:
        st8 = fresh("use4")
        grants.grant(st8.props, int(sel["itemId"]), 1, tx=Tx(st8.props), source="test")
        _f8, err8, _t8 = item_use.apply(st8, int(sel["itemId"]), 1, {})
        check("返回 15006", err8 is not None and err8.code == 15006, err8)
        still = [g for g in st8.props["item_bag"].values()
                 if isinstance(g, dict) and int(g.get("itemId") or 0) == int(sel["itemId"])]
        check("道具还在", len(still) == 1, len(still))
    else:
        print("  (数据里没有 selectItem 道具，跳过)")

    print("\n[10] 盲盒（randomItem）：使用 -> 盒子里出道具，盒子被扣")
    from random import Random

    from game_server.property.bag import pools
    pools.RNG = Random(2026)              # 固定随机源，测试可重复
    box = None
    pid = 0
    for _it in items:
        if _it.get("useType") != "randomItem":
            continue
        _kl = (_it.get("useParam") or {}).get("KeyList") or []
        if len(_kl) != 1:
            continue
        try:
            _d = pools.draw(None, int(_kl[0]), 1, rng=Random(2026))
        except Exception:                  # noqa: BLE001 — 池缺失的道具跳过
            continue
        if _d.items:                       # 选一个真能出道具的盲盒
            box, pid = _it, int(_kl[0])
            break
    if box:
        st10 = fresh("use5")
        before_items = {g.get("itemId") for g in st10.props["item_bag"].values() if isinstance(g, dict)}
        if box.get("autoUse"):
            # autoUse 盲盒：发放即开，不留格子（正确的语义）
            out10 = grants.grant(st10.props, int(box["itemId"]), 1, tx=Tx(st10.props), source="test")
            check("盲盒发放成功", out10.ok, out10)
        else:
            grants.grant(st10.props, int(box["itemId"]), 1, tx=Tx(st10.props), source="test")
            before_items = {g.get("itemId") for g in st10.props["item_bag"].values() if isinstance(g, dict)}
            _f10, err10, _t10 = item_use.apply(st10, int(box["itemId"]), 1, {})
            check("盲盒使用成功", err10 is None, err10)
        after_items = {g.get("itemId") for g in st10.props["item_bag"].values() if isinstance(g, dict)}
        gained = after_items - before_items - {int(box["itemId"])}
        check("盒子里出了东西", bool(gained), sorted(gained)[:5])
        check("盲盒没有留在包里",
              not any(isinstance(g, dict) and int(g.get("itemId") or 0) == int(box["itemId"])
                      for g in st10.props["item_bag"].values()))
        check("池 id 与数据一致", pools.draw(None, pid, 1, rng=Random(1)) is not None, pid)
    else:
        print("  (数据里没有 randomItem 道具，跳过)")

    print("\n[9] E2E：服务端产出的结构必须能被客户端 Lua 解析")
    try:
        import copy as _copy

        from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_as_client

        if not os.path.isdir(LUA_ROOT):
            raise ImportError("没有客户端 Lua 目录")
        lua = new_runtime()
        boot_config(lua)
        ok9, err9 = parse_as_client(lua, "ItemInfo", _copy.deepcopy(grids[0]))
        check("发放的格子在客户端解析通过", ok9, err9)
        ok10, err10 = parse_as_client(lua, "Fashion", _copy.deepcopy(got))
        check("装饰条目在客户端解析通过", ok10, err10)
    except ImportError as exc:
        print(f"  (跳过 E2E：{exc})")

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
