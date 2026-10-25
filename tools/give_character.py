#!/usr/bin/env python3
"""给玩家发角色（离线改存档）。

用法
----
    python tools/give_character.py --uid 15900159000 --char 1020
    python tools/give_character.py --uid 15900159000 --char 1020 --slot 2
    python tools/give_character.py --uid 15900159000 --char 1020 --dry-run

说明
----
- 角色 id 在 BDD 的 CharData 里（如 1020 = 无常）。
- 从存档里已有的角色条目**复制一份结构**当模板（保证字段完整），
  换掉 useSkinId 和所有 __id__，再以新 charId 为键插进 characterBag。
- --slot N 会把这个角色放进编队第 N 格（1..3），方便直接切人。
- **需要重新登录游戏才生效**。
"""
import argparse
import copy
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402

INIT_JSON = os.path.join(_ROOT, "game_server", "avatar_init.json")


def load_props(uid):
    """取玩家属性：优先存档，否则用 initDict。"""
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
#   不更新 props 的 _oo_next_id -> 发完角色重登后第一次分配就撞号。
#   现在统一走 property.ids.alloc_id（全项目唯一发号入口）。
from game_server.property import ids as _ids  # noqa: E402


def max_id(obj, cur=0):
    """兼容旧调用：委托给 property.ids.max_id。"""
    return _ids.max_id(obj, cur)


def reid(obj, props):
    """递归给所有 __id__ 换新值（走统一分配器，会推进 props 里的计数器）。"""
    if isinstance(obj, dict):
        if "__id__" in obj:
            obj["__id__"] = _ids.alloc_id(props)
        for k, v in list(obj.items()):
            reid(v, props)
    elif isinstance(obj, list):
        for v in obj:
            reid(v, props)
    return obj


def give_char(props, char_id, slot=None):
    """返回 (是否新增, 说明)。"""
    bag = props.get("characterBag")
    if not isinstance(bag, dict):
        raise SystemExit("characterBag 属性不存在")

    if char_id in bag or str(char_id) in bag:
        if slot:
            _put_formation(props, char_id, slot)
        return False, "已经有这个角色了"

    # 找一个已有角色当模板
    template = None
    for k, v in bag.items():
        if k in ("__tp__", "__id__"):
            continue
        if isinstance(v, dict):
            template = v
            break
    if template is None:
        raise SystemExit("存档里没有任何角色可当模板，先用 initDict 起一份存档")

    cd = gamedata.table("CharData") or {}
    info = cd.get(str(char_id)) or cd.get(char_id) or {}
    default_skin = info.get("DefaultSkin")
    if not default_skin:
        raise SystemExit("CharData 里找不到角色 {} 的 DefaultSkin（id 对吗？）".format(char_id))

    newbie = reid(copy.deepcopy(template), props)
    newbie["useSkinId"] = int(default_skin)
    bag[char_id] = newbie

    if slot:
        _put_formation(props, char_id, slot)
    return True, "已添加（默认皮肤 {}）".format(default_skin)


def _put_formation(props, char_id, slot):
    """把角色放进编队第 slot 格（1..3）。"""
    cl = props.get("character_list")
    if not isinstance(cl, dict):
        raise SystemExit("character_list 属性不存在")
    team = cl.get(1) or cl.get("#1") or cl.get("1")
    if not isinstance(team, dict):
        raise SystemExit("编队第 1 队不存在")
    lst = team.get("value")
    if not isinstance(lst, dict):
        raise SystemExit("编队 value 不是字典")
    lst["#%d" % slot] = int(char_id)


def main():
    ap = argparse.ArgumentParser(description="给玩家发角色（离线改存档）")
    ap.add_argument("--uid", default="OO00000001")
    ap.add_argument("--char", type=int, required=True, help="角色 id（CharData 的 charId）")
    ap.add_argument("--slot", type=int, default=None, help="放进编队第几格（1..3）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    props, src = load_props(args.uid)
    print("来源: %s（属性 %d 条）" % (src, len(props)))

    added, msg = give_char(props, args.char, args.slot)
    print("角色 {}：{}".format(args.char, msg))
    if args.slot:
        print("已放入编队第 %d 格" % args.slot)

    if args.dry_run:
        print("--dry-run，不写盘")
        return 0

    st = pstate.PlayerState(uid=args.uid, props=props)
    if not st.save():
        raise SystemExit("写盘失败: {}".format(st.path()))
    print("已写入 {}".format(st.path()))
    print("提示：需要重新登录游戏才生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())