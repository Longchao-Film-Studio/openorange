#!/usr/bin/env python
"""特殊目标效果测试：街头声望 / 角色好感（送礼）。

这两类效果的落点不在背包里：
  · 街头声望 -> street_info[streetId].prestige_exp（street.prestige 唯一入口）
  · 角色好感 -> favor_exp_map / popularity_map
入口有两个：UseItem_CS（带 ext.charId）和 AddCharFavorExp_CS（送礼面板）。

用法: python tests/test_bag_special.py
"""
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_special_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.property.bag import grants, item_use  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid):
    props = copy.deepcopy(initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"]))
    return pstate.PlayerState(uid=uid, props=props)


def items_of(use_type):
    return [v for v in gamedata.table("ItemData").values()
            if isinstance(v, dict) and v.get("useType") == use_type]


def run():
    print("\n[1] 街头声望道具：使用后 prestige_exp 增加")
    pres = items_of("streetPrestige")
    check("有街街头声望道具", bool(pres), len(pres))
    if pres:
        it = pres[0]
        p = it.get("useParam") or {}
        sid = int(p.get("streetId") or 0)
        exp = int(p.get("prestigeExp") or 0)
        st = fresh("pres1")
        si = st.props.get("street_info")
        check("street_info 存在", isinstance(si, dict))
        before = int(((si or {}).get(sid) or {}).get("prestige_exp") or 0) if isinstance(si, dict) else 0
        if it.get("autoUse"):
            # 街头声望道具是 autoUse：发放即生效，不留格子
            out = grants.grant(st.props, int(it["itemId"]), 1, tx=Tx(st.props), source="test")
            check("发放成功", out.ok, out)
        else:
            grants.grant(st.props, int(it["itemId"]), 1, tx=Tx(st.props), source="test")
            _frames, err, _tail = item_use.apply(st, int(it["itemId"]), 1, {})
            check("使用成功", err is None, err)
        after = int(((st.props.get("street_info") or {}).get(sid) or {}).get("prestige_exp") or 0)
        check(f"声望经验 +{exp}", after == before + exp, (before, after))
        check("道具不在包里（已消耗/自动使用）",
              not any(isinstance(g, dict) and int(g.get("itemId") or 0) == int(it["itemId"])
                      for g in st.props["item_bag"].values()))

    print("\n[2] 好感道具：缺 ext.charId 不消耗（15006）")
    fav = items_of("charFavorSpace")
    check("有好感道具", bool(fav), len(fav))
    if fav:
        it = fav[0]
        st = fresh("fav1")
        grants.grant(st.props, int(it["itemId"]), 2, tx=Tx(st.props), source="test")
        _f, err, _t = item_use.apply(st, int(it["itemId"]), 1, {})
        check("返回 15006", err is not None and err.code == 15006, err)
        still = [g for g in st.props["item_bag"].values()
                 if isinstance(g, dict) and int(g.get("itemId") or 0) == int(it["itemId"])]
        check("道具还在", len(still) == 1 and int(still[0]["num"]) == 2, still)

        print("\n[3] 好感道具：带 ext.charId 生效")
        char_id = 1001
        before_f = int((st.props.get("favor_exp_map") or {}).get(char_id) or 0)
        before_p = int((st.props.get("popularity_map") or {}).get(char_id) or 0)
        _f3, err3, _t3 = item_use.apply(st, int(it["itemId"]), 1, {"charId": char_id})
        check("使用成功", err3 is None, err3)
        pr = it.get("useParam") or {}
        check("favor_exp 增加", int(st.props["favor_exp_map"].get(char_id) or 0) == before_f + int(pr.get("CharFavorExp") or 0),
              st.props["favor_exp_map"].get(char_id))
        check("popularity 增加", int(st.props["popularity_map"].get(char_id) or 0) == before_p + int(pr.get("CharPopularity") or 0),
              st.props["popularity_map"].get(char_id))

        print("\n[4] AddCharFavorExp_CS（送礼面板路径）：扣礼 + 回 favor_exp/popularity")
        st4 = fresh("fav2")
        iid = int(it["itemId"])
        grants.grant(st4.props, iid, 3, tx=Tx(st4.props), source="test")
        pstate.STATES["fav2"] = st4          # 让 dispatch 的 state(uid) 拿到这份状态
        from game_server import dispatch as _dispatch
        idx = __import__("game_server.rpc_index", fromlist=["RPC_INDEX"]).RPC_INDEX["AddCharFavorExp_CS"]
        frames4 = _dispatch.handle(idx, msmod.mp_arr([2001, {iid: 2}]), 7, uid="fav2")
        check("有应答帧", bool(frames4), len(frames4))
        resp = None
        for f in frames4:
            try:
                _t, method, _rid, params = msmod.parse_entity_response(f)
            except Exception:
                continue
            if method == "__rpc_response__":
                resp = params
        check("应答带 favor_exp/popularity",
              isinstance(resp, list) and len(resp) >= 2
              and "favor_exp" in resp[1] and "popularity" in resp[1], resp)
        left = [g for g in st4.props["item_bag"].values()
                if isinstance(g, dict) and int(g.get("itemId") or 0) == iid]
        check("扣了 2 个礼", int(left[0]["num"]) == 1 if left else True, left)

        print("\n[5] E2E：favor_exp_map（IntDictKeyInt）交给客户端 Lua 解析")
        try:
            from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_as_client

            if not os.path.isdir(LUA_ROOT):
                raise ImportError("没有客户端 Lua 目录")
            lua = new_runtime()
            boot_config(lua)
            ok, err5 = parse_as_client(lua, "IntDictKeyInt",
                                       {"__id__": 5001, 1001: 88})
            check("favor_exp_map 结构解析通过", ok, err5)
        except ImportError as e:
            print(f"  (跳过 E2E：{e})")

    print("\n[6] 通行证经验（BpExp / BpExpSeason）：镜像 bpinfo.lua add_bp_exp")
    st6 = fresh("bp1")
    bp_item = (gamedata.item(3) or {})
    season_item = (gamedata.item(7) or {})
    check("找到 BpExp 道具", (bp_item.get("useParam") or {}).get("BpExp") is not None, bp_item.get("itemId"))
    if bp_item.get("useParam"):
        add = int(bp_item["useParam"].get("BpExp") or 0)
        info0 = st6.props["bp_info"]
        b0, a0, w0 = (int(info0.get(k) or 0) for k in ("bp_exp", "all_bp_exp", "week_bp_exp"))
        out6 = grants.grant(st6.props, 3, 1, tx=Tx(st6.props), source="test")
        info1 = st6.props["bp_info"]
        check("发放成功", out6.ok, out6)
        check("bp_exp 增加", int(info1["bp_exp"]) == b0 + add, (b0, info1.get("bp_exp")))
        check("all_bp_exp 增加", int(info1["all_bp_exp"]) == a0 + add, (a0, info1.get("all_bp_exp")))
        check("week_bp_exp 增加（非赛季经验）", int(info1["week_bp_exp"]) == w0 + add, (w0, info1.get("week_bp_exp")))
        check("bp_exp_time 刷新", int(info1.get("bp_exp_time") or 0) > 0, info1.get("bp_exp_time"))

    if season_item.get("useParam"):
        add7 = int(season_item["useParam"].get("BpExpSeason") or 0)
        info2 = st6.props["bp_info"]
        # 注意：info2 是 props 里的同一份 dict，必须先取数值再发放
        b2 = int(info2.get("bp_exp") or 0)
        w1 = int(info2.get("week_bp_exp") or 0)
        out7 = grants.grant(st6.props, 7, 1, tx=Tx(st6.props), source="test")
        info3 = st6.props["bp_info"]
        check("赛季经验发放成功", out7.ok, out7)
        check("bp_exp 再增加", int(info3["bp_exp"]) == b2 + add7, (b2, info3.get("bp_exp")))
        check("赛季经验**不**加 week_bp_exp", int(info3["week_bp_exp"]) == w1, (w1, info3.get("week_bp_exp")))

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
