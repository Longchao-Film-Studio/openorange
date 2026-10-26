#!/usr/bin/env python
"""过期清理 + 奖励展示（bag/expiry.py、bag/feedback.py）测试。

覆盖：
  · 过期道具格子被删、最近过期记录可供 QueryExpiredItems 返回
  · 过期装饰按 FashionType 记进 expired_fashions（客户端读这个展示"已过期"）
  · 未过期的（expireTime=0 永久）不能误删
  · 登录路径（live_init_dict）真的会清理
  · Avatar_Reward_SC 帧：方法名/索引/参数形状 {itemId:{num,gridids}}
  · E2E：expired_fashions 结构交给客户端 Lua 解析通过

用法: python tests/test_bag_expiry.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_expire_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server.property.bag import expiry, feedback  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def props_with(items=None, fashions=None):
    return {
        "item_bag": {"__id__": 100, **(items or {})},
        "portrait_bag": {"__id__": 200, **(fashions or {})},
        "expired_fashions": {"__id__": 300},
    }


NOW = 1_700_000_000


def run():
    print("\n[1] 过期道具格子被删，未过期的保留")
    props = props_with({
        "g1": {"__id__": 1, "id": "g1", "itemId": 101, "num": 5, "expireTime": NOW - 10},
        "g2": {"__id__": 2, "id": "g2", "itemId": 102, "num": 7, "expireTime": 0},
        "g3": {"__id__": 3, "id": "g3", "itemId": 103, "num": 1, "expireTime": NOW + 999},
    })
    res = expiry.sweep(props, now=NOW)
    bag_now = props["item_bag"]
    check("过期格子被删", "g1" not in bag_now, list(bag_now))
    check("永久道具保留", "g2" in bag_now)
    check("未到期道具保留", "g3" in bag_now)
    check("清理结果记录了 itemId/num",
          res.items == [{"itemId": 101, "num": 5}], res.items)

    print("\n[2] 最近过期清单可返回（客户端 response.expired_items）")
    hist = expiry.expired_items(props)
    check("清单元素形如 {itemId, num}",
          hist and hist[0]["itemId"] == 101 and hist[0]["num"] == 5, hist)

    print("\n[3] 过期装饰：从 bag 删除并记进 expired_fashions[type][id]")
    props = props_with(fashions={
        513: {"__id__": 11, "id": 513, "expiration_time": NOW - 1},
        601: {"__id__": 12, "id": 601, "expiration_time": 0},
    })
    res = expiry.sweep(props, now=NOW)
    check("过期装饰被删", 513 not in props["portrait_bag"], props["portrait_bag"])
    check("永久装饰保留", 601 in props["portrait_bag"])
    ef = expiry.expired_fashions(props)
    check("记进 FashionType=1（portrait）", ef.get(1, {}).get(513) == NOW, ef)
    _raw = props["expired_fashions"]
    check("内层是自定义字典（客户端 roinit 要 __id__）",
          isinstance(_raw.get(1), dict) and bool(_raw[1].get("__id__")), _raw.get(1))
    from game_server.property import validate as _validate
    _missing = _validate.find_missing_ids(props)
    check("过期记录写入后没有缺 __id__", not _missing, _missing[:3])

    print("\n[4] 登录路径（live_init_dict）会真的清理过期")
    from game_server import player_state as pstate
    from game_server.ms import initdict

    st = pstate.get_state(initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"]),
                          uid="expire_login")
    st.props["item_bag"]["gexp"] = {"__id__": 9001, "id": "gexp", "itemId": 101,
                                    "num": 2, "expireTime": 1000}
    st.save()
    pstate.STATES.pop("expire_login", None)          # 强制重新读盘
    initdict.live_init_dict("expire_login")
    st2 = pstate.STATES["expire_login"]
    check("登录后过期格子不在了", "gexp" not in st2.props["item_bag"], list(st2.props["item_bag"]))

    print("\n[5] Avatar_Reward_SC 帧形状")
    frames = feedback.reward_frames([(101, 5, ["g1"]), (102, 2, [])])
    check("产出 1 帧", len(frames) == 1, len(frames))
    pkt = frames[0]
    _t, method, _rid, params = msmod.parse_entity_response(pkt)
    check("方法名 = Avatar_Reward_SC", method == "Avatar_Reward_SC", method)
    check("参数是 1 个 map", isinstance(params, list) and len(params) == 1, params)
    m = params[0]
    check("map 键是道具 id", 101 in m and 102 in m, m)
    check("带 num", int(m[101]["num"]) == 5, m[101])
    check("唯一实例带 gridids", list(m[101]["gridids"]) == ["g1"], m[101])
    check("普通道具不带 gridids", "gridids" not in m[102], m[102])
    check("空发放不产帧", feedback.reward_frames([]) == [])

    print("\n[6] E2E：expired_fashions 结构交给客户端 Lua 解析")
    try:
        from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_as_client

        if not os.path.isdir(LUA_ROOT):
            raise ImportError("没有客户端 Lua 目录")
        lua = new_runtime()
        boot_config(lua)
        # IntDictDictKeyInt：外层字典 + 内层字典都必须是带 __id__ 的自定义对象
        ef_clean = {"__id__": 300, 1: {"__id__": 301, 513: NOW}}
        ok, err = parse_as_client(lua, "IntDictDictKeyInt", ef_clean)
        check("expired_fashions 类型解析通过", ok, err)
    except ImportError as e:
        print(f"  (跳过 E2E：{e})")

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
