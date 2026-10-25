#!/usr/bin/env python3
"""实体帧解析 / 应答的抓包回归测试。

用法:
    python tests/test_entity_rpc_frames.py

★ 这里所有字节都是 **实机抓到的原始帧**（work/oo_analysis/gamesrv_runX.log
  里 [GATE] RX 段的十六进制），不是构造出来的。改帧格式解析就靠这套兜底。
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ 雷2：本套件会触发未实现 RPC（兜底分支），日志/存档都别写进真实目录
import tempfile  # noqa: E402

os.environ.setdefault("OO_STATE_DIR", tempfile.mkdtemp(prefix="oo_test_rpc_state_"))
os.environ["OO_UNKNOWN_LOG"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_ulog_"), "unknown_requests.tsv")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config as ooconf
from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

passed = []
failed = []

def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))

def h(s):
    return bytes.fromhex(s.replace(" ", ""))

# ---- 实机抓到的单帧 ----
F_SYNC_TIME = h("0F 00 00 00 04 00 18 94 BD A1 07 22 06 91 CE 6A D6 1C 3C")
F_HOTFIX = h("0C 00 00 00 04 00 18 AF 97 B1 DD 07 22 02 91 A0")
# RpcGetMatchDuration，rid=1
F_MATCHDUR_RID1 = h("0D 00 00 00 04 00 18 E9 A1 89 D8 06 22 01 90 28 01")
# RpcGetSunActLevel_CS，rid=2
F_SUNACT_RID2 = h("0D 00 00 00 04 00 18 B3 C0 ED EC 03 22 01 90 28 02")
F_HEARTBEAT0 = h("0C 00 00 00 04 00 18 A4 84 E1 D8 04 22 02 91 00")

# ---- 实机抓到的「两帧粘在一个 TCP 段里」的 77 字节 ----
F_TWO = h(
    "39 00 00 00 04 00 18 A0 CE 97 41 22 2E "
    "91 84 A7 73 74 6F 72 79 49 44 01 A6 6D 6F 64 65 49 64 01 "
    "AB 73 79 73 74 65 6D 5F 74 79 70 65 CD 03 EC AA 6D 61 74 63 68 5F 74 79 70 65 03 "
    "28 03 "
    "0C 00 00 00 04 00 18 A4 84 E1 D8 04 22 02 91 00"
)

def parse_response(pkt):
    """应答 = entityMessage(type=2) + methodName __rpc_response__ + field4/5。

    这是扫客户端进程内存拿到 proto 描述符之后确定的格式，
    见 game_server/ms.py 里 build_entity_response 的注释。
    """
    plen = struct.unpack_from("<I", pkt, 0)[0]
    assert len(pkt) == 4 + plen, (len(pkt), plen)
    mtype, method, rid, val = msmod.parse_entity_response(pkt)
    assert method == "__rpc_response__", method
    return mtype, rid, val

def run():
    print("\n[1] 单帧解析（实机字节）")
    idx, params, rid = msmod.parse_entity_frame(F_SYNC_TIME[6:])
    check("RpcSyncServerTime index=15228564", idx == 15228564, idx)
    check("RpcSyncServerTime rid=0", rid == 0, rid)
    val, _used = msmod.mp_decode(params)
    check("参数是 [客户端时间]", isinstance(val, list) and len(val) == 1, val)
    check("客户端时间是整数", isinstance(val[0], int) and val[0] > 0, val)

    idx, params, rid = msmod.parse_entity_frame(F_HOTFIX[6:])
    check("RpcPullHotfix index=2074889135", idx == 2074889135, idx)
    check("RpcPullHotfix rid=0", rid == 0, rid)
    # ★ 注意：91 A0 里 A0 是 fixstr(0)，也就是空字符串，不是空 map（空 map 是 80）。
    #   对应 basecomp.lua:8 CallServer("RpcPullHotfix", mgr.hotfix_md5)，
    #   hotfix_md5 是空串而不是 nil。
    check("RpcPullHotfix 参数是 [\"\"]", msmod.mp_decode(params)[0] == [""])

    idx, params, rid = msmod.parse_entity_frame(F_MATCHDUR_RID1[6:])
    check("RpcGetMatchDuration index=1795313897", idx == 1795313897, idx)
    check("RpcGetMatchDuration rid=1", rid == 1, rid)
    check("RpcGetMatchDuration 参数是 []", msmod.mp_decode(params)[0] == [])

    idx, params, rid = msmod.parse_entity_frame(F_SUNACT_RID2[6:])
    check("RpcGetSunActLevel_CS index=1033592883", idx == 1033592883, idx)
    check("RpcGetSunActLevel_CS rid=2", rid == 2, rid)
    check("RpcGetSunActLevel_CS 参数是 []", msmod.mp_decode(params)[0] == [])

    idx, params, rid = msmod.parse_entity_frame(F_HEARTBEAT0[6:])
    check("心跳 index=1259880996", idx == 1259880996, idx)
    check("心跳 rid=0", rid == 0, rid)
    check("心跳参数是 [0]", msmod.mp_decode(params)[0] == [0])

    print("\n[2] 两帧粘连（实机 77 字节）")
    frames, rest = msmod.split_frames(F_TWO)
    check("切成 2 帧", len(frames) == 2, len(frames))
    check("长度 61 + 16", [len(f) for f in frames] == [61, 16], [len(f) for f in frames])
    check("没有剩余字节", rest == b"", rest)
    i1, _, r1 = msmod.parse_entity_frame(frames[0][6:])
    i2, _, r2 = msmod.parse_entity_frame(frames[1][6:])
    check("第 1 帧是 Cmd_Avatar_StartGame_CS", i1 == 136701728, i1)
    check("第 1 帧 rid=3", r1 == 3, r1)
    check("第 2 帧是心跳 index=0", i2 == 1259880996 and r2 == 0, (i2, r2))

    print("\n[3] 半帧 / 空缓冲")
    frames, rest = msmod.split_frames(F_TWO[:30])
    check("半帧不返回", frames == [], frames)
    check("半帧进剩余缓冲", rest == F_TWO[:30], len(rest))
    frames, rest = msmod.split_frames(b"")
    check("空缓冲不返回", frames == [] and rest == b"")
    frames, rest = msmod.split_frames(F_TWO[:5])
    check("不足 6 字节不返回", frames == [] and len(rest) == 5)

    print("\n[4] 用实机帧跑应答（帧类型 + rid 回带）")
    mtype, rid, val = parse_response(guarded_handle(1033592883, b"\x90", 1)[0])
    check("应答帧类型 = 2（entityMessage）",
          mtype == msmod.ENTITY_RESPONSE_FRAME_TYPE,
          (mtype, msmod.ENTITY_RESPONSE_FRAME_TYPE))
    check("RpcGetSunActLevel rid=1 回带", rid == 1, rid)
    check("RpcGetSunActLevel 回 levelIds", val[1].get("levelIds") == [], val[1])

    mtype, rid, val = parse_response(guarded_handle(1795313897, b"\x90", 3)[0])
    check("RpcGetMatchDuration rid=3 回带", rid == 3, rid)
    check("RpcGetMatchDuration 回 data", val[1].get("data") == {}, val[1])

    mtype, rid, val = parse_response(guarded_handle(1674960337, b"\x90", 4)[0])
    check("QueryMyTeamInfo rid=4 回带", rid == 4, rid)
    check("QueryMyTeamInfo status=false", val[0].get("status") is False, val[0])

    print("\n[4b] 心跳帧 type=6 不是连接帧（实机字节）")
    # 0A 00 00 00 06 00 08 C8 92 C8 EB AA AE 21
    #   type=6，field1 = 纳秒时间戳（sendHeartbeat）
    # 旧代码因为「field 1 存在」就当成 connect 帧回 ConnectServerResp，
    # 客户端收到莫名的 Connected 就断开重连。判断连接帧必须看 frame_type == 3。
    F_HEARTBEAT6 = h("0A 00 00 00 06 00 08 C8 92 C8 EB AA AE 21")
    frames, rest = msmod.split_frames(F_HEARTBEAT6)
    check("心跳帧可切分", len(frames) == 1 and rest == b"", (len(frames), rest))
    mtype = struct.unpack_from("<H", frames[0], 4)[0]
    check("心跳帧 type=6（不是 3）", mtype == 6, mtype)
    f1 = msmod.pb_parse(frames[0][6:])
    check("field1 是纳秒时间戳（不是 reqType）", f1[1][0] > 1e12, f1.get(1))

    print("\n[5] 单向帧不应答")
    for tag, frame in (("RpcPullHotfix", F_HOTFIX), ("心跳", F_HEARTBEAT0),
                       ("RpcSyncServerTime", F_SYNC_TIME)):
        idx, params, rid = msmod.parse_entity_frame(frame[6:])
        check(tag + "（rid=0）返回空列表", guarded_handle(idx, params, rid) == [])

    print("\n[6] 阶段 1 的 RPC 都能用通用 ACK 应答（兼容模式）")
    # ★ 雷2：未实现 RPC 的兜底现在分模式。这一段测的是兼容模式行为
    #   （UpgradePlayerStage_CS / BreakCharacter_CS 未实现）。
    ooconf.configure_unknown_fake_ok(True)
    # 抽 6 条 1-1/1-2/1-3 的 RPC，用空数组参数模拟调用
    # ★ 升级/使用语义修复后不再是"通用 ACK"：
    #   UpgradeCharacter_CS 要真材料、UseItem_CS 要真道具，空参数回失败是正确行为。
    #   这里改为「真数据调用 + 断言应答帧形状」，BreakCharacter 等未实现的仍走兼容 ACK。
    msmod._live_init_dict("frames_t6")   # 先建完整存档
    _pst = __import__("game_server.player_state", fromlist=["get_state"]).get_state(uid="frames_t6")
    _ib = _pst.props.setdefault("item_bag", {})
    _ib["991"] = {"__id__": 990011, "id": "991", "itemId": 201, "num": 9}
    _ib["992"] = {"__id__": 990012, "id": "992", "itemId": 101, "num": 9}
    _pst.set("item_bag", _ib)
    for nm, args in (("UpgradeCharacter_CS", [9001, {"201": 1}]),
                     ("BreakCharacter_CS", [9001]),
                     ("UseItem_CS", [101, 1, None]),
                     ("SubmitItem_CS", [None, {"101": 1}]),
                     ("SetSignature_CS", ["x"]),
                     ("UpgradePlayerStage_CS", [])):
        idx = erpc.RPC_INDEX[nm]
        fr = guarded_handle(idx, msmod.mp_arr(args), 9)
        # ★ 修复后 UpgradeCharacter/SetSignature 会带属性同步帧。
        #   respond_after_sync 的顺序 = syncs 在前 + 应答在后 —— 找含
        #   __rpc_response__ 的应答帧来解析
        pkt = None
        for f in (fr if isinstance(fr, list) else [fr]):
            if isinstance(f, (bytes, bytearray)) and b"__rpc_response__" in f:
                pkt = f
                break
        if pkt is None:
            pkt = fr[0] if isinstance(fr, list) and fr else fr
        mtype, rid, val = parse_response(pkt)
        ok = (mtype == msmod.ENTITY_RESPONSE_FRAME_TYPE and rid == 9
              and "status" in (val[0] if isinstance(val, list) and val else {}))
        check(nm + " 应答帧形状", ok, (mtype, rid, val))

    ooconf.configure_unknown_fake_ok(False)

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
