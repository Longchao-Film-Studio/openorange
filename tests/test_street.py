#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 2：街区切换测试。

逆向依据：
  entities/avatarcomponent/avatarstreetcomp.lua:107-128, 651-668
    enterStreet_CS(id)         -> RpcEnterStreet {id, streetParams}
    AvatarStreet_SwitchStreet  -> RequestSwitchStreet_CS {streetId, streetParams}
    leaveStreet_CS(id)         -> RequestLeaveStreet_CS {id}

  小地图数据源 ui/panel/mappanel/node_map_small/node_map_smallgroup.lua:30
    StreetData[GlobalData.Avatar:getStreetID()]
  所以服务端必须改 street_id，否则场景换了地图不换。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_street_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, entity_rpc as erpc, player_state as pstate, config

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


# ★ 原来这里有个 parse_sync 依赖 msmod.parse_property_sync，但那个函数根本不存在，
#   永远返回 {}（死代码）。现在改成用影子缓存做真正的帧校验。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_cache import frames_safe   # noqa: E402


def run():
    print("\n[1] 索引表里有街区三条")
    for n in ("RequestSwitchStreet_CS", "RpcEnterStreet", "RequestLeaveStreet_CS"):
        check(n + " 在 RPC_INDEX", n in erpc.RPC_INDEX)
        check(n + " 有处理器", erpc.RPC_INDEX.get(n) in erpc._HANDLERS)

    print("\n[2] 切换到火锅店 1011")
    config.configure_scaffold(unlock_all=True)
    msmod._live_init_dict("street_test")
    st = pstate.get_state(uid="street_test")
    check("初始 street_id = 101", st.props.get("street_id") == 101, st.props.get("street_id"))
    _pre = msmod._live_init_dict("street_test")   # RPC 前快照（审计 P0-3）
    frames = erpc.handle(erpc.RPC_INDEX["RequestSwitchStreet_CS"],
                         msmod.mp_arr([1011, None]), 1, uid="street_test")
    check("street_id -> 1011", st.props.get("street_id") == 1011, st.props.get("street_id"))
    check("返回 >= 3 帧（应答 + street_id + street_info）", len(frames) >= 3, len(frames))
    # ★ 影子缓存校验：这些帧喂进客户端会不会崩
    _sc, _errs = frames_safe(_pre, frames, "切街区")
    check("切街区的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n[3] street_info 里建了 1011 条目")
    si = st.props.get("street_info") or {}
    check("有 1011 键", 1011 in si, list(si.keys())[:5])
    e = si.get(1011) or {}
    check("__tp__ = StreetInfo", e.get("__tp__") == "StreetInfo", e.get("__tp__"))
    check("带 __id__", isinstance(e.get("__id__"), int), e.get("__id__"))
    for f in ("prestige_street_id", "prestige_level", "prestige_exp",
              "surface_switch_enter_time", "surface_switch_can_enter_time", "cd_choose"):
        check("字段 " + f + " 存在", f in e)

    print("\n[4] __id__ 不能和已有属性撞")
    from game_server.property import ids as _ids
    mx = _ids.max_id({k: v for k, v in st.props.items() if k != "street_info"})
    check("新条目 __id__ > 已有最大值", e.get("__id__") > mx, (e.get("__id__"), mx))

    print("\n[5] RpcEnterStreet 走同一条路")
    st.props["street_id"] = 101
    st.set("street_id", 101)
    erpc.handle(erpc.RPC_INDEX["RpcEnterStreet"],
                msmod.mp_arr([100, None]), 2, uid="street_test")
    check("street_id -> 100（龙潮阁）", st.props.get("street_id") == 100, st.props.get("street_id"))
    check("street_info 有 100", 100 in (st.props.get("street_info") or {}))

    print("\n[6] 离开街区")
    erpc.handle(erpc.RPC_INDEX["RequestLeaveStreet_CS"],
                msmod.mp_arr([100]), 3, uid="street_test")
    check("street_id -> 0", st.props.get("street_id") == 0, st.props.get("street_id"))

    print("\n[7] 参数非法时不崩")
    before = st.props.get("street_id")
    erpc.handle(erpc.RPC_INDEX["RequestSwitchStreet_CS"], msmod.mp_arr([]), 4,
                uid="street_test")
    check("空参数不改数据", st.props.get("street_id") == before)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())