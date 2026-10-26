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
- **发放走 property.bag.grants 唯一入口**（堆叠 / maxNum / notStack / 钥匙规范 /
  ext / 过期时间 / autoUse 全在这里），本工具不再自己拼格子；
- 写回 work/player_state/<uid>.json；
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

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.property import ids as _ids  # noqa: E402
from game_server.property.bag import grants  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402

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
        with open(INIT_JSON, encoding="utf-8") as f:
            d = json.load(f)
        props = msmod.conv_int_keys(d["__Properties__"])
        src = "initDict"
    props["uid"] = uid
    return props, src


def max_id(obj, cur=0):
    """兼容旧调用：委托给 property.ids.max_id。"""
    return _ids.max_id(obj, cur)


def give(props, item_id, num):
    """发 num 个 item_id（走 grants 唯一入口），返回 (格子键, 当前数量, 说明)。

    · 叠加/新格/堆叠上限由 grants 决定；
    · autoUse 道具会被立即使用（没有格子）——返回空键 + "自动使用"；
    · 失败（id 不在 ItemData 等）抛 RuntimeError，调用方不写盘。
    """
    tx = Tx(props)
    outcome = grants.grant(props, int(item_id), int(num), tx=tx, source="tool:give_item")
    if not outcome.ok:
        raise RuntimeError(str(outcome.error))
    if not outcome.grids:
        return "", 0, "自动使用"
    key = str(outcome.grids[-1])
    bag = props.get("item_bag") or {}
    grid = bag.get(key)
    total = int(grid.get("num") or 0) if isinstance(grid, dict) else 0
    how = "新格子" if outcome.new_grids else "叠加"
    return key, total, how


def main():
    ap = argparse.ArgumentParser(description="给玩家发道具（离线改存档）")
    ap.add_argument("--uid", default="OO00000001")
    ap.add_argument("--item", type=int, required=True, help="道具 id")
    ap.add_argument("--num", type=int, default=1, help="数量")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.num <= 0:
        raise SystemExit("--num 必须 > 0")

    props, src = load_props(args.uid)
    print(f"来源: {src}（属性 {len(props)} 条）")

    try:
        key, total, how = give(props, args.item, args.num)
    except RuntimeError as e:
        # 发放失败要把原因原样告诉用户（例如 15002 ItemData 里没有这个道具）
        raise SystemExit(f"发放失败: {e}") from e
    if key:
        print(f'道具 {args.item}：{how} -> 格子 "{key}"，当前数量 {total}')
    else:
        print(f"道具 {args.item}：{how}（autoUse 道具发放即消耗，没有格子）")

    if args.dry_run:
        print("--dry-run，不写盘")
        return 0

    st = pstate.PlayerState(uid=args.uid, props=props)
    if not st.save():
        raise SystemExit(f"写盘失败: {st.path()}")
    print(f"已写入 {st.path()}")
    print("提示：需要重新登录游戏才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
