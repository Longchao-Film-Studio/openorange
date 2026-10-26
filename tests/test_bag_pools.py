#!/usr/bin/env python
"""随机池（bag/pools.py）测试。

数据驱动 + 统计学断言：randType 的语义是**从数据推断**的（客户端没有随机池逻辑），
所以这里直接对全表统计特征做断言，语义一改测试就红：

    randType=1  相对权重（0~2500）  -> 每次抽**一个**
    randType=2  绝对概率（≤1，99.6%）-> 逐条独立，可能 0~N 个
    maxTimesLimit 1~3              -> 按日限次

用法: python tests/test_bag_pools.py
"""
import os
import sys
import tempfile
from random import Random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_pools_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server.property.bag import pools  # noqa: E402
from game_server.property.bag.errors import BagError  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def all_pools():
    """{pool_id: {autoId: entry}} —— 直接从 BDD 读，用于统计断言。"""
    raw = gamedata.table("RandomItem")
    out = {}
    for pid, entries in raw.items():
        if isinstance(entries, dict):
            out[int(pid)] = entries
    return out


def find_pool(pred, what):
    for pid, entries in all_pools().items():
        if pred(entries):
            return pid, entries
    raise SystemExit("找不到池子：" + what)


def run():
    print("\n[1] 池子表可读")
    ps = all_pools()
    check("池子数 > 700", len(ps) > 700, len(ps))

    print("\n[2] randType 语义的数据依据（语义改了这里必须红）")
    w1, w2 = [], []
    for entries in ps.values():
        for e in entries.values():
            if not isinstance(e, dict):
                continue
            try:
                w = float(e.get("weight") or 0)
            except (TypeError, ValueError):
                continue
            (w1 if e.get("randType") == 1 else w2 if e.get("randType") == 2 else []).append(w)
    check("randType=1 有权重 > 1 的条目（相对权重）", max(w1) > 1, max(w1))
    check("randType=2 权重几乎都 ≤ 1（概率）",
          sum(1 for w in w2 if w <= 1) / max(1, len(w2)) > 0.99,
          sum(1 for w in w2 if w <= 1) / max(1, len(w2)))

    print("\n[3] randType=1：每次抽恰好一个条目")
    pid1, _ = find_pool(lambda es: len(es) >= 3 and all(
        isinstance(e, dict) and e.get("randType") == 1 and isinstance(e.get("itemList"), dict)
        for e in es.values()) and any(float(e.get("weight") or 0) > 0 for e in es.values()),
                          "type1 全 itemList 且有正权重")
    d1 = pools.draw(None, pid1, 5, rng=Random(7))
    check("5 次抽取 -> 5 个 autoId", len(d1.top_auto_ids) == 5, len(d1.top_auto_ids))
    check("有可入包道具或已记录非道具", bool(d1.items or d1.others), (d1.items, d1.others))

    print("\n[4] randType=2：逐条独立（weight=1 时全中）")
    cands = [(pid, es) for pid, es in ps.items()
             if len(es) >= 2 and all(
                 isinstance(e, dict) and e.get("randType") == 2
                 and float(e.get("weight") or 0) >= 1 for e in es.values())]
    check("存在 type2 全权重>=1 的池", bool(cands), len(cands))
    bad = []
    for pid, es in cands:
        d = pools.draw(None, pid, 1, rng=Random(7))
        if len(d.top_auto_ids) != len(es):
            bad.append((pid, len(d.top_auto_ids), len(es)))
    check("这些池一次抽满（独立概率 weight>=1 必中）", not bad, bad[:3])

    print("\n[5] 嵌套池（subPool）能递归解析")
    sub_pid, _sub_es = find_pool(lambda es: any("subPool" in e for e in es.values() if isinstance(e, dict)),
                                "含 subPool")
    d3 = pools.draw(None, sub_pid, 20, rng=Random(11))
    check("抽出了东西（道具或非道具）", bool(d3.items or d3.others), (d3.items, d3.others))

    print("\n[6] 不存在的池 -> 15007（调用方不扣道具）")
    err = None
    try:
        pools.draw(None, 99999999, 1)
    except BagError as e:
        err = e
    check("抛 Bag_InvaildItemList", err is not None and err.code == 15007, err)

    print("\n[7] maxTimesLimit：按日计数，达到上限后不允许再抽中")
    # 直接验计数器语义（抽中与否是概率事件，统计断言会抖）
    props = {}
    check("首次允许", pools._limit_ok(props, 777, 1) is True)
    pools._hit(props, 777)
    check("用掉 1 次后不允许", pools._limit_ok(props, 777, 1) is False)
    check("上限 2 时仍允许", pools._limit_ok(props, 777, 2) is True)
    props2 = {"_oo_draw_limits": {"888": {"d": "19700101", "n": 99}}}
    check("跨天自动重置", pools._limit_ok(props2, 888, 1) is True)
    check("无上限时不限制", pools._limit_ok(props, 999, None) is True)

    print("\n[8] 数据驱动：170 条 randomItem 道具引用的池")
    items = [v for v in gamedata.table("ItemData").values()
             if isinstance(v, dict) and v.get("useType") == "randomItem"]
    check("randomItem 道具 170 条", len(items) == 170, len(items))
    bad_pools, empty_ok, ok_pools = [], [], 0
    for it in items:
        kl = ((it.get("useParam") or {}).get("KeyList") or [])
        if len(kl) != 1:
            bad_pools.append((it["itemId"], kl))
            continue
        pid = int(kl[0])
        try:
            d = pools.draw(None, pid, 1, rng=Random(5))
        except BagError:
            bad_pools.append((it["itemId"], pid))
            continue
        if d.items:
            ok_pools += 1
        else:
            empty_ok.append((it["itemId"], pid, len(d.others)))
    check("引用的池基本都存在（坏的进清单）", len(bad_pools) <= 6, bad_pools)
    check("绝大多数池能抽出可入包道具", ok_pools > len(items) * 0.8, (ok_pools, len(items)))
    print(f"     可抽出道具 {ok_pools} / 只有非道具 {len(empty_ok)} / 池缺失 {len(bad_pools)}")

    print("\n[9] E2E：同源 Lua 能解析抽到的道具 id 对应的 ItemInfo")
    try:
        from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_as_client
        if not os.path.isdir(LUA_ROOT):
            raise ImportError("没有客户端 Lua 目录")
        lua = new_runtime()
        boot_config(lua)
        iid = None
        for _it in items:
            _kl = ((_it.get("useParam") or {}).get("KeyList") or [])
            if len(_kl) != 1:
                continue
            try:
                _d = pools.draw(None, int(_kl[0]), 1, rng=Random(9))
            except BagError:
                continue
            if _d.items:
                iid = _d.items[0][0]
                break
        check("抽到的道具 id 在 ItemData 里", iid is not None and isinstance(gamedata.item(iid), dict), iid)
        grid = {"__id__": 1, "id": "g1", "itemId": int(iid), "num": 1,
                "getTime": 0.0, "expireTime": 0.0, "ext": {"__id__": 2}}
        ok, err = parse_as_client(lua, "ItemInfo", grid)
        check("抽到的道具能过客户端解析", ok, err)
    except ImportError as e:
        print(f"  (跳过 E2E：{e})")

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
