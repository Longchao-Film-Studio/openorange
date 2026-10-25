#!/usr/bin/env python3
"""给玩家发道具（离线改存档）。

用法
----
    python tools/give_item.py --uid OO00000001 --item 110 --num 5
    python tools/give_item.py --uid OO00000001 --item 110 --num 5 --dry-run

说明
----
- 有存档就基于存档改；存档不存在或损坏（属性 < 10 条）就从
  game_server/avatar_init.json 起一份；
- 会自动分配新的格子键（"3"、"4"…）和全局唯一的 __id__；
- 写回 work/player_state/<uid>.json。
- **需要重新登录游戏才生效**（服务端只在登录时读存档）。

为什么不用 /give 聊天指令
------------------------
聊天指令要走 gate 的 channelMessage / 聊天 RPC，那条链路还没打通；
离线改存档是当前最可靠的方式。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server import player_state as pstate
from game_server import ms as msmod

INIT_JSON = os.path.join(_ROOT, "game_server", "avatar_init.json")


def load_props(uid):
    """取玩家属性：优先存档，否则用 initDict。

    ★ 不管走哪条路，都要把 props["uid"] 覆盖成 uid。
    以前从 initDict 起状态时没覆盖，导致存档里出现
    「顶层 uid=15900159000，props.uid=OO00000001」这种自相矛盾的数据 ——
    而客户端读的正是 props.uid。
    """
    st = pstate.load(uid)
    if st is not None:
        props = st.props
        src = "存档"
    else:
        with open(INIT_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
        props = msmod._conv_int_keys(d["__Properties__"])
        src = "initDict"
    props["uid"] = uid
    return props, src


# ★ 2026-10-21 审计：这里原本自带一份 max_id + 自己发 __id__，
#   不更新 props 的 _oo_next_id -> 发完道具重登后第一次分配就撞号。
#   现在统一走 property.ids.alloc_id（全项目唯一发号入口）。
from game_server.property import ids as _ids


def max_id(obj, cur=0):
    """兼容旧调用：委托给 property.ids.max_id。"""
    return _ids.max_id(obj, cur)


def give(props, item_id, num, grid_key=None):
    """往 item_bag 里加 num 个 item_id，返回实际写入的格子键。"""
    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        raise SystemExit("item_bag 属性不存在")

    # 已经有这个道具就叠加数量
    for k, grid in bag.items():
        if k in ("__tp__", "__id__"):
            continue
        if isinstance(grid, dict) and grid.get("itemId") == item_id:
            grid["num"] = int(grid.get("num") or 0) + int(num)
            return k, int(grid["num"]), "叠加"

    # 新开一格
    used = set()
    for k in bag.keys():
        if k in ("__tp__", "__id__"):
            continue
        try:
            used.add(int(k))
        except Exception:
            pass
    nk = grid_key
    if nk is None:
        nk = 1
        while nk in used:
            nk += 1
    nk = str(nk)

    grid = {
        "__id__": _ids.alloc_id(props),
        "id": nk,
        "itemId": int(item_id),
        "num": int(num),
        "expireTime": 0.0,
        "getTime": 0.0,
        "ext": {"__id__": _ids.alloc_id(props)},
    }
    bag[nk] = grid
    return nk, int(num), "新格子"


def main():
    ap = argparse.ArgumentParser(description="给玩家发道具（离线改存档）")
    ap.add_argument("--uid", default="OO00000001")
    ap.add_argument("--item", type=int, required=True, help="道具 id")
    ap.add_argument("--num", type=int, default=1, help="数量")
    ap.add_argument("--grid", default=None, help="指定格子键（默认自动分配）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.num <= 0:
        raise SystemExit("--num 必须 > 0")

    props, src = load_props(args.uid)
    print("来源: %s（属性 %d 条）" % (src, len(props)))

    key, total, how = give(props, args.item, args.num, args.grid)
    print("道具 %d：%s -> 格子 \"%s\"，当前数量 %d" % (args.item, how, key, total))

    if args.dry_run:
        print("--dry-run，不写盘")
        return 0

    st = pstate.PlayerState(uid=args.uid, props=props)
    if not st.save():
        raise SystemExit("写盘失败: %s" % st.path())
    print("已写入 %s" % st.path())
    print("提示：需要重新登录游戏才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
