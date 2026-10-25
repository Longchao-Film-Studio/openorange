#!/usr/bin/env python3
"""ms 层帧构造 / 解析的全量冒烟测试（P0-E3 拆分后新增）。

为什么要有这个文件：
    ms.py 拆成 ms/ 包之后，原有单测只覆盖了「帧解析」和「业务 RPC」，
    没有一条直接调用 msmod 的帧**构造器**。结果 build_channel_entity 在
    搬运时少带了一个 import（pb_bytes_field）：单测 980 项全绿，
    实机一登录就报
        [GATE] #012 error: name 'pb_bytes_field' is not defined
    客户端卡在 createChannelEntity 前，绑定链直接断。

    所以这里把 msmod 的**每一个公开构造器**都真调一遍 —— 专治拆包后的漏 import。

用法: python tests/test_ms_frames.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def call(name, fn, *args, **kw):
    """调用构造器并断言返回非空 bytes（漏 import 会在这里抛 NameError）。"""
    try:
        out = fn(*args, **kw)
    except Exception as e:
        check(name + " 不抛异常", False, f"{type(e).__name__}: {e}")
        return None
    check(name + " 不抛异常", True)
    check(name + " 返回非空 bytes", isinstance(out, bytes) and len(out) > 0,
          type(out))
    return out


def run():
    print("\n[1] protobuf / msgpack 编解码")
    b = msmod.pb_varint_field(1, 300)
    check("pb_varint_field 编码", isinstance(b, bytes) and len(b) > 0, b)
    b2 = msmod.pb_bytes_field(2, b"abc")
    check("pb_bytes_field 编码", isinstance(b2, bytes) and len(b2) > 0, b2)
    fields = msmod.pb_parse(b + b2)
    check("pb_parse 读回 field1=300", fields.get(1) == [300], fields)
    check("pb_parse 读回 field2=abc", fields.get(2) == [b"abc"], fields)
    check("pb_read_varint 单字节", msmod.pb_read_varint(b"\x05", 0) == (5, 1))
    for v in (1, -1, 3.5, True, "s", [1, 2], {"a": 1}):
        enc = msmod.mp_any(v)
        dec, used = msmod.mp_decode(enc)
        check(f"msgpack 往返 {v!r}", dec == v and used == len(enc),
              (dec, used, len(enc)))
    check("mp_map 往返", msmod.mp_decode(msmod.mp_map({"a": 1}))[0] == {"a": 1})
    check("mp_arr 往返", msmod.mp_decode(msmod.mp_arr([1, "x"]))[0] == [1, "x"])
    check("_conv_int_keys 去 # 号", msmod._conv_int_keys({"#9001": 1}) == {9001: 1})

    print("\n[2] 帧封装与拆帧")
    pkt = msmod.frame(3, b"\x08\x01")
    check("frame 头 4 字节长度 + 2 字节类型",
          len(pkt) == 6 + 2 and pkt[4:6] == b"\x03\x00", pkt[:6])
    frames, rest = msmod.split_frames(pkt + pkt)
    check("split_frames 切出 2 帧", len(frames) == 2 and rest == b"",
          (len(frames), rest))
    frames2, rest2 = msmod.split_frames(pkt + pkt[:3])
    check("split_frames 留尾巴", len(frames2) == 1 and len(rest2) == 3,
          (len(frames2), len(rest2)))

    print("\n[3] 实体：createChannelEntity / entityMessage / 应答")
    ch = call("build_channel_entity", msmod.build_channel_entity,
              entity_id="avatar_15900159000", session_id="15900159000",
              uid="15900159000")
    if ch:
        check("createChannelEntity 帧类型=4", ch[4:6] == b"\x04\x00", ch[:6])
    em = call("build_entity_message", msmod.build_entity_message,
              method_name="RpcOnEnterSpace", index=1879036833,
              params=msmod.mp_arr([1000, "", {}]))
    if em:
        check("entityMessage 帧类型=2", em[4:6] == b"\x02\x00", em[:6])
    resp = call("build_entity_response", msmod.build_entity_response, 42,
                ret={"status": True}, resp={})
    if resp:
        mtype, method, rid, val = msmod.parse_entity_response(resp)
        check("应答 methodName=__rpc_response__", method == "__rpc_response__", method)
        check("应答 rid 原样带回", rid == 42, rid)
        check("应答 retStatus.status=True", val[0].get("status") is True, val)
    idx, params, rid = msmod.parse_entity_frame(em[6:] if em else b"")
    check("parse_entity_frame 取回 index", idx == 1879036833, idx)
    check("parse_entity_frame 取回 rid=0", rid == 0, rid)
    call("build_time_sync", msmod.build_time_sync, 111, 222)
    call("build_net_check_ack", msmod.build_net_check_ack, 7)
    call("build_sunact_level_sc", msmod.build_sunact_level_sc, 9)
    call("build_match_duration_sc", msmod.build_match_duration_sc, 9)
    call("build_team_info_sc", msmod.build_team_info_sc, 9)

    print("\n[4] 属性同步帧（5 个 opType）")
    for op_name, op in (("OP_CHANGE", msmod.OP_CHANGE), ("OP_ADD", msmod.OP_ADD),
                        ("OP_DEL", msmod.OP_DEL), ("OP_INSERT", msmod.OP_INSERT),
                        ("OP_REMOVE", msmod.OP_REMOVE)):
        val = {"__tp__": "ItemBag", "__id__": 1, "1": 2} if op == msmod.OP_ADD else 1
        call("build_property_sync " + op_name, msmod.build_property_sync,
             "", 0, op, "signature", val)
    check("PROPERTY_SYNC_TYPE=13", msmod.PROPERTY_SYNC_TYPE == 13)
    ps = msmod.build_property_sync("", 0, msmod.OP_CHANGE, "signature", "x")
    check("属性同步帧类型=13", ps[4:6] == b"\x0d\x00", ps[:6])

    print("\n[5] 绑定链（bindAccount / bindAvatar / bindSoul）")
    call("build_entity_mailbox", msmod.build_entity_mailbox,
              entity_id="account_x")
    call("build_bind_account", msmod.build_bind_account, auth="a")
    call("frame_bind_account", msmod.frame_bind_account, auth="a")
    call("build_server_info", msmod.build_server_info)
    call("build_bind_avatar", msmod.build_bind_avatar, auth="a")
    call("frame_bind_avatar", msmod.frame_bind_avatar, auth="a")
    call("build_bind_soul", msmod.build_bind_soul, auth="a")
    call("frame_bind_soul", msmod.frame_bind_soul, auth="a")
    check("bindAccount 帧类型=6", msmod.frame_bind_account(auth="a")[4:6] == b"\x06\x00")
    check("bindAvatar 帧类型=7", msmod.frame_bind_avatar(auth="a")[4:6] == b"\x07\x00")
    check("bindSoul 帧类型=8", msmod.frame_bind_soul(auth="a")[4:6] == b"\x08\x00")

    print("\n[6] ms 应答 / 连接应答 / ServerList / load service / userdata")
    call("build_connect_server_resp", msmod.build_connect_server_resp, 1, "OO00000001")
    call("frame_connect_resp", msmod.frame_connect_resp, 1, "OO00000001")
    call("build_ms_response", msmod.build_ms_response, 5,
         msmod.mp_map({"Ret": [True, 0]}))
    call("frame_ms_response", msmod.frame_ms_response, 5,
         msmod.mp_map({"Ret": [True, 0]}))
    sl_json = msmod.build_server_list_json(33101)
    check("build_server_list_json 返回非空 str",
          isinstance(sl_json, str) and len(sl_json) > 0, type(sl_json))
    if isinstance(sl_json, str) and sl_json:
        import json
        obj = json.loads(sl_json)
        check("ServerList 有 servers + regions",
              isinstance(obj.get("servers"), list) and isinstance(obj.get("regions"), list),
              list(obj))
    call("build_response_params", msmod.build_response_params, 33101)
    call("build_load_service_params", msmod.build_load_service_params, 33101)
    ud = call("build_userdata_batch_params", msmod.build_userdata_batch_params,
              [["15900159000", ["signature", "level"]]])
    if ud:
        dec = msmod.mp_decode(ud)[0]
        check("batchGetAttribute 回 Results",
              isinstance(dec.get("Results"), list) and dec["Results"], dec)

    print("\n[7] initDict / 登录 tick")
    check("AVATAR_INIT_DICT 有 __Properties__",
          isinstance(msmod.AVATAR_INIT_DICT.get("__Properties__"), dict))
    init = msmod._live_init_dict(uid="__ms_frame_test__")
    check("_live_init_dict 返回 __Properties__",
          isinstance(init.get("__Properties__"), dict), type(init))
    check("initDict 顶层无 _oo_ 字段",
          not [k for k in init["__Properties__"] if str(k).startswith("_oo_")])
    # 清理测试账号的状态文件，别留垃圾
    try:
        from game_server import player_state as pstate
        pstate.forget("__ms_frame_test__")
    except Exception:
        pass

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
