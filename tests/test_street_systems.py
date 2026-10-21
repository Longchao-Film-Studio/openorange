#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段 2：街景声望 + 派遣 + AOI 表情。

逆向依据：
  entities/avatarcomponent/avatarstreetcomp.lua:
    137-143  RequestGetPrestigeAward_CS {streetId, level} / RequestAddPrestigeExp_CS {exp}
     99-101  RequestGetPrestigeTaskAward_CS {streetId, prestigeTaskId}
    277-300  RequestDispatchChar_CS / RequestFinishDispatch_CS / RequestCancelDispatchChar_CS

  StreetInfo（config.lua:6834）字段：prestige_street_id / prestige_level /
  prestige_exp / prestige_awarded / prestige_tasks；升级阈值在 BDD StreetPrestigeData。
  StreetDispatchInfo：street_id / dispatch_id / char_id / beg_time / end_time。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_streetx_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, entity_rpc as erpc, player_state as pstate, config, gamedata

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] 索引与处理器")
    for n in ("RequestAddPrestigeExp_CS", "RequestGetPrestigeAward_CS",
              "RequestGetPrestigeTaskAward_CS", "RequestDispatchChar_CS",
              "RequestFinishDispatch_CS", "RequestCancelDispatchChar_CS"):
        check(n + " 在索引表", n in erpc.RPC_INDEX)
        check(n + " 有处理器", erpc.RPC_INDEX.get(n) in erpc._HANDLERS)
    check("RpcPlayActEmoji 是单向处理器", 2000707256 in erpc._ONE_WAY_HANDLERS)

    print("\n[2] StreetPrestigeData 能读到升级阈值")
    rows = gamedata.street_prestige(101)
    check("街区 101 有声望表", len(rows) > 0, len(rows))
    if rows:
        check("第一条有 needExpNum", isinstance(rows[0].get("needExpNum"), int),
              rows[0].get("needExpNum"))

    print("\n[3] 加声望经验会升级")
    config.configure_scaffold(unlock_all=True)
    msmod._live_init_dict("sx")
    st = pstate.get_state(uid="sx")
    st.props["street_id"] = 101
    need1 = int((rows[0] or {}).get("needExpNum") or 100)
    _pre = msmod._live_init_dict("sx")   # RPC 前快照（审计 P0-3）
    frames = erpc.handle(erpc.RPC_INDEX["RequestAddPrestigeExp_CS"],
                         msmod.mp_arr([need1 + 30]), 1, uid="sx")
    entry = (st.props.get("street_info") or {}).get(101) or {}
    check("等级升到 1", entry.get("prestige_level") == 1, entry.get("prestige_level"))
    check("剩余经验 30", entry.get("prestige_exp") == 30, entry.get("prestige_exp"))
    check("返回同步帧", len(frames) >= 2, len(frames))
    # ★ 影子缓存校验：加声望的帧喂进客户端会不会崩
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shadow_cache import frames_safe
    _sc, _errs = frames_safe(_pre, frames, "加声望")
    check("加声望的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n[4] 领声望等级奖励")
    frames = erpc.handle(erpc.RPC_INDEX["RequestGetPrestigeAward_CS"],
                         msmod.mp_arr([101, 1]), 2, uid="sx")
    entry = (st.props.get("street_info") or {}).get(101) or {}
    check("prestige_awarded[1] = 1",
          (entry.get("prestige_awarded") or {}).get(1) == 1,
          entry.get("prestige_awarded"))

    print("\n[5] 领声望任务奖励")
    erpc.handle(erpc.RPC_INDEX["RequestGetPrestigeTaskAward_CS"],
                msmod.mp_arr([101, 777]), 3, uid="sx")
    entry = (st.props.get("street_info") or {}).get(101) or {}
    check("prestige_tasks[777] = 1",
          (entry.get("prestige_tasks") or {}).get(777) == 1,
          entry.get("prestige_tasks"))

    print("\n[6] 派遣：派出 -> 完成 -> 取消")
    frames = erpc.handle(erpc.RPC_INDEX["RequestDispatchChar_CS"],
                         msmod.mp_arr([9001, 5001]), 4, uid="sx")
    d = st.props.get("street_dispatch") or {}
    e1 = d.get(5001) or {}
    check("派遣条目存在", bool(e1), list(d.keys())[:5])
    check("__tp__ = StreetDispatchInfo", e1.get("__tp__") == "StreetDispatchInfo",
          e1.get("__tp__"))
    check("char_id = 9001", e1.get("char_id") == 9001, e1.get("char_id"))
    check("beg/end_time 都有", e1.get("beg_time") and e1.get("end_time"), e1)
    check("返回同步帧", len(frames) >= 2, len(frames))
    erpc.handle(erpc.RPC_INDEX["RequestFinishDispatch_CS"],
                msmod.mp_arr([5001]), 5, uid="sx")
    check("完成后 end_time <= beg_time+1",
          (d.get(5001) or {}).get("end_time", 0) <= (d.get(5001) or {}).get("beg_time", 0) + 1,
          d.get(5001))
    erpc.handle(erpc.RPC_INDEX["RequestCancelDispatchChar_CS"],
                msmod.mp_arr([5001]), 6, uid="sx")
    check("取消后条目消失", 5001 not in d, list(d.keys())[:5])

    print("\n[7] AOI 表情不崩")
    out = erpc.handle(2000707256, msmod.mp_arr([5]), 0, uid="sx")
    check("单向返回空列表", out == [], out)
    check("记录了 emoji", st.props.get("_oo_last_emoji") == 5,
          st.props.get("_oo_last_emoji"))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())