#!/usr/bin/env python3
"""给「核对背包系统」用的测试账号塞一套可一眼确认的样本（离线改存档）。

用法
----
    python tools/seed_bagcheck.py --uid bagcheck
    python tools/seed_bagcheck.py --uid bagcheck --dry-run

塞进去的东西（每项对应一条人工核对点，见 docs/验证-实机启动.md §2）：

  1. 盲盒（randomItem）×3            -> 开一个应弹出「获得物品」面板、盒子消失
  2. 装饰道具（avatarAttr Portrait） -> 头像装饰里多一项（autoUse，发放即生效）
  3. 不支持效果道具（charSkin）      -> 使用时提示不可用且**不消耗**（15009）
  4. 街头声望道具（autoUse）         -> 对应街区声望经验增加
  5. 好感/送礼道具 ×2                -> 送礼面板送 1 个，好感+人气增加
  6. 通行证经验道具（item 3）        -> bp_exp / 周经验增加
  7. 已过期道具（expireTime 已过）   -> 登录后从背包消失，进「过期道具」清单
  8. 已过期装饰（expiration_time 过）-> 进「过期装饰」清单（个人信息页）

★ 7/8 的过期时间是**测试种子**故意写过去的（正常发放不会立刻过期）。
"""
import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.property.bag import classify, grants, item_use, pools  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402

INIT_JSON = os.path.join(_ROOT, "game_server", "avatar_init.json")


def load_props(uid):
    st = pstate.load(uid)
    if st is not None:
        return st.props, "存档"
    with open(INIT_JSON, encoding="utf-8") as f:
        d = json.load(f)
    return msmod.conv_int_keys(d["__Properties__"]), "initDict"


def pick(pred, what):
    for it in gamedata.table("ItemData").values():
        if isinstance(it, dict) and it.get("itemId") and pred(it):
            return it
    raise SystemExit("找不到样本：" + what)


def give(props, item_id, num, rows, note):
    tx = Tx(props)
    oc = grants.grant(props, int(item_id), int(num), tx=tx, source="seed:bagcheck")
    status = "ok" if oc.ok else ("拒绝 " + str(oc.error))
    rows.append((int(item_id), gamedata.item(int(item_id)).get("name"), num, note, status))
    return oc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="bagcheck")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true", help="忽略已有存档，从 initDict 重新生成")
    args = ap.parse_args()

    if args.reset:
        _save = os.path.join(_ROOT, "var", "saves", args.uid + ".json")
        if os.path.exists(_save):
            os.replace(_save, _save + ".bak")
            print(f"已有存档已备份到 {_save}.bak")
    props, src = load_props(args.uid)
    props["uid"] = args.uid
    rows = []

    # 1) 盲盒：选一个「池子里真能开出道具」的
    box = None
    for it in gamedata.table("ItemData").values():
        if (not isinstance(it, dict) or it.get("useType") != "randomItem"
                or it.get("autoUse")):
            continue
        kl = (it.get("useParam") or {}).get("KeyList") or []
        if len(kl) != 1:
            continue
        try:
            d = pools.draw(None, int(kl[0]), 1)
        except Exception:
            continue
        if d.items:
            box = it
            break
    if box is None:
        raise SystemExit("找不到能开出道具的盲盒")
    give(props, box["itemId"], 3, rows, "盲盒：开 1 个应弹获得面板")

    # 2) 装饰（各 bag 各来一个，避免同 id 冲突）
    deco = [it for it in gamedata.table("ItemData").values()
            if isinstance(it, dict) and it.get("useType") == "avatarAttr"
            and "Portrait" in ((it.get("useParam") or {}).get("KeyList") or [])]
    if deco:
        give(props, deco[0]["itemId"], 1, rows, "头像装饰：装饰页应多一项")
    if len(deco) > 1:
        give(props, deco[1]["itemId"], 1, rows, "头像装饰（第二件，留给过期测试）")

    # 3) 不支持效果的道具（应按 15009 拒绝且不消耗）
    bad = pick(lambda it: classify.classify(it.get("useType")) == classify.UNSUPPORTED,
               "不支持效果的 useType")
    give(props, bad["itemId"], 2, rows, "不支持效果：用它会提示不可用且不消耗")

    # 4) 街头声望（autoUse，发放即加声望）
    pres = pick(lambda it: it.get("useType") == "streetPrestige", "streetPrestige")
    give(props, pres["itemId"], 1, rows, "声望道具：对应街区声望经验 +")

    # 5) 好感/送礼道具 ×2
    fav = pick(lambda it: it.get("useType") == "charFavorSpace", "charFavorSpace")
    give(props, fav["itemId"], 2, rows, "送礼：送礼面板送给某个角色，好感/人气 +")

    # 6) 通行证经验（item 3）
    give(props, 3, 1, rows, "通行证经验：bp_exp / 周经验 +")

    # 7) 已过期道具：先按正常语义发放，再把 expireTime 写过去（测试种子）
    # 注意：ItemData 里没有配 validTime 的道具（全 0），所以直接给一个普通道具、
    # 再把格子的 expireTime 写到过去 —— 这是**测试种子**，不是业务逻辑。
    # 选一个「starter 背包里没有」的可叠加道具，避免把新手整堆货币过期掉
    _starter_ids = {int(g.get("itemId") or 0)
                    for g in (props.get("item_bag") or {}).values()
                    if isinstance(g, dict)}
    exp_item = pick(lambda it: not it.get("autoUse") and not it.get("notStack")
                    and int(it.get("itemId")) not in _starter_ids,
                    "普通可叠加道具（过期测试用）")
    oc = give(props, exp_item["itemId"], 1, rows, "过期道具：登录后应消失并进过期清单")
    if oc.ok and oc.grids:
        grid = (props.get("item_bag") or {}).get(oc.grids[-1])
        if isinstance(grid, dict):
            grid["expireTime"] = float(int(time.time()) - 3600)

    # 8) 已过期装饰：把装饰条目的 expiration_time 改到过去（测试种子）
    if len(deco) > 1:
        bag_name = item_use.DECO_BAGS["Portrait"]
        fbag = props.get(bag_name) or {}
        for k, v in fbag.items():
            if str(k).startswith("__") or not isinstance(v, dict):
                continue
            if int(v.get("id") or 0) == int((deco[1].get("useParam") or {}).get("Portrait") or 0):
                v["expiration_time"] = float(int(time.time()) - 3600)
                break

    print(f"来源: {src}（属性 {len(props)} 条），uid={args.uid}")
    print()
    print(f"{'道具 id':>8}  {'名称':<14} {'数量':>4}  说明")
    for iid, name, num, note, status in rows:
        print(f"{iid:>8}  {str(name)[:12]:<14} {num:>4}  {note}  [{status}]")

    if args.dry_run:
        print("\n--dry-run，不写盘")
        return 0

    st = pstate.PlayerState(uid=args.uid, props=props)
    if not st.save():
        raise SystemExit("写盘失败")
    print(f"\n已写入 {st.path()}")
    print("登录游戏后生效；核对清单见 docs/验证-实机启动.md §2。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
