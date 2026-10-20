#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实体 RPC 应答层测试（阶段 1 地基）。

用法:
    python tests/test_entity_rpc.py

只测纯函数，不需要启动服务、不需要游戏客户端。
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import entity_rpc as erpc  # noqa: E402
from game_server import ms as msmod  # noqa: E402

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def split_frame(pkt):
    """拆 [u32 LE payloadLen][u16 LE msgType][body]，返回 (msg_type, body)。"""
    plen = struct.unpack_from("<I", pkt, 0)[0]
    assert len(pkt) == 4 + plen, (len(pkt), plen)
    mtype = struct.unpack_from("<H", pkt, 4)[0]
    return mtype, pkt[6:]


def parse_response(pkt):
    """解析应答帧，返回 (mtype, rid, params)。

    ★ 应答格式是逆向出来的（扫客户端进程内存拿到的 proto 描述符）：
      type=2 entityMessage，field2="__rpc_response__"，field4=msgpack，field5=rid。
    """
    mtype, method, rid, params = msmod.parse_entity_response(pkt)
    assert method == "__rpc_response__", method
    return mtype, rid, params


def run():
    print("\n[1] 索引表完整性")
    # 42 = 1-1(9) + 1-2(4) + 1-3(22) + 登录链路(7)；再加编队 4 条 = 46
    # 42 = 1-1(9)+1-2(4)+1-3(22)+登录链路(7)；+编队 4=46；+街区 3=49；+剧情 3=52
    check("条目数 57", len(erpc.RPC_INDEX) == 57, len(erpc.RPC_INDEX))
    check("索引全不重复", len(set(erpc.RPC_INDEX.values())) == len(erpc.RPC_INDEX))
    check("索引全为正数", all(v > 0 for v in erpc.RPC_INDEX.values()))
    check("反查表一致",
          all(erpc.name_of(v) == k for k, v in erpc.RPC_INDEX.items()))

    print("\n[2] 阶段 1 三个组件的条数（和 ROADMAP 对得上）")
    char_keys = [k for k in erpc.RPC_INDEX
                 if k.startswith(("Cmd_Avatar_Skin", "Cmd_Avatar_Unlock",
                                  "Cmd_Avatar_UseCharSkin"))
                 or k in ("UpgradeCharacter_CS", "BreakCharacter_CS",
                          "FetchBreakAward_CS", "RisingStarCharacter_CS")]
    bag_keys = [k for k in erpc.RPC_INDEX
                if k in ("UseItem_CS", "RecordItemObtain_CS",
                         "SubmitItem_CS", "RpcGiftCodeRedeem")]
    # 编队 RPC 也以 Set 开头，但属于 avatarformationcomp，不算进 1-3。
    FORMATION = ("SetChoosenIndex_CS", "SetChoosenChar_CS",
                 "SetCharacterList_CS", "SetCharacterListName_CS")
    user_keys = [k for k in erpc.RPC_INDEX
                 if (k.startswith("Set") or k in (
                     "RetSetHasScore_CS", "ForbidSelf_CS", "RpcSetTextHideState",
                     "RpcQueryExpiredFashions", "QueryExpiredItems_CS",
                     "UpgradePlayerStage_CS"))
                 and k not in FORMATION]
    formation_keys = [k for k in erpc.RPC_INDEX if k in FORMATION]
    check("1-1 角色 9 条", len(char_keys) == 9, len(char_keys))
    check("1-2 背包 4 条", len(bag_keys) == 4, len(bag_keys))
    check("1-3 玩家数据 22 条", len(user_keys) == 22, len(user_keys))
    check("编队 4 条", len(formation_keys) == 4, len(formation_keys))

    print("\n[3] 已知名字的索引（和 hash_rpc_names.py 算出来的一致）")
    check("UpgradeCharacter_CS", erpc.RPC_INDEX["UpgradeCharacter_CS"] == 732022161)
    check("BreakCharacter_CS", erpc.RPC_INDEX["BreakCharacter_CS"] == 1158350933)
    check("UseItem_CS", erpc.RPC_INDEX["UseItem_CS"] == 168315907)
    check("SetPortrait_CS", erpc.RPC_INDEX["SetPortrait_CS"] == 40460550)
    check("RpcGetSunActLevel_CS", erpc.RPC_INDEX["RpcGetSunActLevel_CS"] == 1033592883)

    print("\n[4] 通用 ACK：帧类型、rid 回带、参数是 [retStatus, response]")
    pkt = erpc.handle(732022161, b"\x90", 7)[0]
    mtype, rid, val = parse_response(pkt)
    # 应答 = entityMessage(type=2) + methodName __rpc_response__（逆向确认）
    check("帧类型 = 2（entityMessage）",
          mtype == msmod.ENTITY_RESPONSE_FRAME_TYPE,
          (mtype, msmod.ENTITY_RESPONSE_FRAME_TYPE))
    check("rid 回带 7", rid == 7, rid)
    check("参数是两元组", isinstance(val, list) and len(val) == 2, val)
    check("retStatus.status 为真", val[0].get("status") is True, val[0])
    check("response 为空表", val[1] == {}, val[1])

    print("\n[5] 单向消息不应答")
    check("rid=0 返回空列表", erpc.handle(732022161, b"\x90", 0) == [])
    check("index=None 返回空列表", erpc.handle(None, b"\x90", 5) == [])

    print("\n[6] 专用应答")
    _, _, v1 = parse_response(erpc.handle(1033592883, b"\x90", 2)[0])
    check("RpcGetSunActLevel 回 levelIds", v1[1].get("levelIds") == [], v1[1])
    _, _, v2 = parse_response(erpc.handle(1795313897, b"\x90", 1)[0])
    check("RpcGetMatchDuration 回 data", v2[1].get("data") == {}, v2[1])
    _, _, v3 = parse_response(erpc.handle(1674960337, b"\x90", 3)[0])
    check("QueryMyTeamInfo 回 status=false", v3[0].get("status") is False, v3[0])

    print("\n[7] 名字解析 / 参数解码")
    check("describe 已知", erpc.describe(1033592883) == "RpcGetSunActLevel_CS")
    check("describe 未知含索引", "999999" in erpc.describe(999999))
    check("decode_params 空数组", erpc.decode_params(b"\x90") == [])
    check("decode_params 坏数据返回 None", erpc.decode_params(b"\xc1") is None)

    print("\n[8] 单向名单里的名字确实在索引表里")
    for nm in erpc.ONE_WAY:
        check("ONE_WAY 包含 " + nm, nm in erpc.RPC_INDEX, nm)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
