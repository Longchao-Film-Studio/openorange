#!/usr/bin/env python
"""阶段 2：空间/场景进出测试。

逆向依据 entities/avatarcomponent/avatarspacecomp.lua：
  EnterSpace(spaceno, spaceid, pos, yaw, info)
    -> CallServer("RpcEnterSpace", spaceno, spaceid or "", info)  单向
  LeaveSpace() -> CallServer("RpcLeaveSpace")                     带回调
  OnSceneLoaded(spaceno) -> CallServer("RpcOnSceneLoaded", spaceno) 单向

服务端要改 space_p 并**主动下发** RpcOnEnterSpace(int,string,table)，
客户端 RpcOnEnterSpace 里才会去加载场景。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_space_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []

def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))

def run():
    print("\n[1] 常量与索引")
    check("SPACE_EDEN = 1000", erpc.SPACE_EDEN == 1000, erpc.SPACE_EDEN)
    check("RpcOnEnterSpace 索引非 0", erpc.RPC_ON_ENTER_SPACE > 0, erpc.RPC_ON_ENTER_SPACE)
    check("RpcLeaveSpace 在索引表", "RpcLeaveSpace" in erpc.RPC_INDEX)
    check("RpcEnterSpace 是单向处理器", 1277075552 in erpc._ONE_WAY_HANDLERS)
    check("RpcOnSceneLoaded 是单向处理器", 996547331 in erpc._ONE_WAY_HANDLERS)

    print("\n[2] 进空间 RpcEnterSpace（单向）")
    config.configure_scaffold()
    msmod._live_init_dict("space_test")
    st = pstate.get_state(uid="space_test")
    sp0 = st.props.get("space_p") or {}
    check("初始 spaceno = 0", sp0.get("spaceno") == 0, sp0.get("spaceno"))
    frames = guarded_handle(1277075552,
                         msmod.mp_arr([1000, "eden_1", {"toPosId": 50004}]), 0,
                         uid="space_test")
    check("返回了 S2C 帧", len(frames) > 0, len(frames))
    sp = st.props.get("space_p") or {}
    check("spaceno -> 1000", sp.get("spaceno") == 1000, sp.get("spaceno"))
    check("seqno +1", sp.get("seqno") == 1, sp.get("seqno"))
    check("spaceid 写入", sp.get("spaceid") == "eden_1", sp.get("spaceid"))
    check("loading = true", sp.get("loading") is True, sp.get("loading"))

    print("\n[3] 下发了 RpcOnEnterSpace")
    # 帧格式：[u32 len][u16 type][body]，type=2 是 entityMessage
    found = False
    for f in frames:
        if len(f) > 6 and f[4] == 2:
            found = True
            check("entityMessage 里带 RpcOnEnterSpace", b"RpcOnEnterSpace" in f, len(f))
            break
    check("确实下发了 entityMessage", found)

    print("\n[4] 场景加载完成 RpcOnSceneLoaded")
    frames = guarded_handle(996547331, msmod.mp_arr([1000]), 0, uid="space_test")
    sp = st.props.get("space_p") or {}
    check("loading -> false", sp.get("loading") is False, sp.get("loading"))
    check("ld_spaceno -> 1000", sp.get("ld_spaceno") == 1000, sp.get("ld_spaceno"))

    print("\n[5] 离开空间 RpcLeaveSpace（带回调）")
    frames = guarded_handle(erpc.RPC_INDEX["RpcLeaveSpace"], msmod.mp_arr([]), 7,
                         uid="space_test")
    check("返回应答帧", len(frames) >= 1, len(frames))
    sp = st.props.get("space_p") or {}
    check("spaceno -> 0", sp.get("spaceno") == 0, sp.get("spaceno"))
    check("seqno +1", sp.get("seqno") == 2, sp.get("seqno"))
    has_tp = any(b"RpcOnTeleportOut" in f for f in frames)
    check("下发了 RpcOnTeleportOut", has_tp)

    print("\n[6] RpcEnterSpace 传 0 等价于离开")
    guarded_handle(1277075552, msmod.mp_arr([1000, "", {}]), 0, uid="space_test")
    guarded_handle(1277075552, msmod.mp_arr([0, "", {}]), 0, uid="space_test")
    sp = st.props.get("space_p") or {}
    check("spaceno -> 0", sp.get("spaceno") == 0, sp.get("spaceno"))

    print("\n[7] 进伊甸 RpcEnterEden / RpcBackToEden")
    guarded_handle(1277075552, msmod.mp_arr([0, "", {}]), 0, uid="space_test")
    frames = guarded_handle(1588890197, msmod.mp_arr([50004]), 0, uid="space_test")
    sp = st.props.get("space_p") or {}
    check("RpcEnterEden -> spaceno = 1000", sp.get("spaceno") == 1000, sp.get("spaceno"))
    check("下发 RpcOnEnterSpace", any(b"RpcOnEnterSpace" in f for f in frames))
    check("info 带 toPosId", any(b"toPosId" in f for f in frames))
    frames = guarded_handle(1369842456, msmod.mp_arr([]), 0, uid="space_test")
    check("RpcBackToEden 也进 1000",
          (st.props.get("space_p") or {}).get("spaceno") == 1000)

    print("\n[8] AOI 移动 RpcOnClientMove")
    guarded_handle(1920167376, msmod.mp_arr([1.5, -3.2, 60.25, 1.57]), 0, uid="space_test")
    pos = st.props.get("_oo_street_pos") or {}
    check("记录 x", abs(pos.get("x", 0) - 1.5) < 1e-6, pos)
    check("记录 z", abs(pos.get("z", 0) - 60.25) < 1e-6, pos)
    check("记录 dir", abs(pos.get("dir", 0) - 1.57) < 1e-6, pos)

    print("\n[9] RpcChangeMicroState")
    guarded_handle(1517096639, msmod.mp_arr([3]), 0, uid="space_test")
    check("micro_state = 3", st.props.get("_oo_micro_state") == 3,
          st.props.get("_oo_micro_state"))

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())