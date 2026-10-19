#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""propertySync2Client 与写操作链路测试。

propertySyncClientMessage 的字段是扫客户端进程内存拿到的 proto 描述符：
  ownerid=1 bytes / propertyid=2 int64 / optype=3 int32 /
  name=4 bytes / value=5 bytes(msgpack) / sessionId=6 bytes
optype 见 core/propertysync/propertytypes.lua:27-33。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod
from game_server import entity_rpc as erpc
from game_server import player_state as pstate

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def parse_sync(pkt):
    """解析 propertySync2Client 帧。"""
    mtype = int.from_bytes(pkt[4:6], "little")
    body = pkt[6:]
    fields = msmod.pb_parse(body)
    out = {"type": mtype}
    for k in (1, 4, 6):
        if k in fields:
            out[{1: "ownerid", 4: "name", 6: "sessionId"}[k]] = fields[k][0].decode("utf-8", "replace")
    if 2 in fields:
        out["propertyid"] = fields[2][0]
    if 3 in fields:
        out["optype"] = fields[3][0]
    if 5 in fields:
        out["value"] = msmod.mp_decode(fields[5][0])[0]
    return out


def run():
    print("\n[1] 属性同步帧格式")
    pkt = msmod.build_property_sync("", 7, msmod.OP_CHANGE, "signature", "OpenOrange")
    d = parse_sync(pkt)
    check("帧类型 = 13", d["type"] == msmod.PROPERTY_SYNC_TYPE, d["type"])
    check("propertyid = 7", d.get("propertyid") == 7, d.get("propertyid"))
    check("optype = 1 (OP_CHANGE)", d.get("optype") == msmod.OP_CHANGE, d.get("optype"))
    check("name = signature", d.get("name") == "signature", d.get("name"))
    check("value 解出来是 OpenOrange", d.get("value") == "OpenOrange", d.get("value"))
    check("ownerid 空串时省略", "ownerid" not in d, d.get("ownerid"))

    print("\n[2] ownerid 非空时带字段 1")
    pkt2 = msmod.build_property_sync("avatar_2000", 1, msmod.OP_CHANGE, "level", 5)
    d2 = parse_sync(pkt2)
    check("ownerid = avatar_2000", d2.get("ownerid") == "avatar_2000", d2.get("ownerid"))
    check("value = 5", d2.get("value") == 5, d2.get("value"))

    print("\n[3] OP_DEL 不带 value")
    pkt3 = msmod.build_property_sync("", 1, msmod.OP_DEL, "x", None)
    d3 = parse_sync(pkt3)
    check("OP_DEL 无 value 字段", "value" not in d3, d3)
    check("optype = 3", d3.get("optype") == msmod.OP_DEL, d3.get("optype"))

    print("\n[4] SetSignature_CS 返回两帧：应答 + 同步")
    pstate.reset_state({"signature": "", "level": 1})
    frames = erpc.handle(msmod.RPC_INDEX_SET_SIGNATURE_CS, msmod.mp_arr(["OpenOrange"]), 42)
    check("返回 2 帧", isinstance(frames, list) and len(frames) == 2, len(frames) if isinstance(frames, list) else frames)
    mtype, method, rid, params = msmod.parse_entity_response(frames[0])
    check("第 1 帧是应答", mtype == 2 and method == "__rpc_response__", (mtype, method))
    check("rid = 42", rid == 42, rid)
    check("应答 status=true", params[0].get("status") is True, params[0])
    d4 = parse_sync(frames[1])
    check("第 2 帧是属性同步", d4["type"] == 13, d4["type"])
    check("同步的是 signature", d4.get("name") == "signature", d4.get("name"))
    check("同步值是 OpenOrange", d4.get("value") == "OpenOrange", d4.get("value"))
    check("服务端状态已改", pstate.get_state().get("signature") == "OpenOrange",
          pstate.get_state().get("signature"))

    print("\n[4b] UseItem_CS 扣道具 + 子属性同步")
    pstate.reset_state({"item_bag": {"__tp__": "ItemBag", "__id__": 1324,
                                      "1": {"__id__": 1357, "id": "1", "itemId": 101,
                                            "num": 1000, "ext": {"__id__": 1358}}}})
    frames3 = erpc.handle(msmod.RPC_INDEX_USE_ITEM_CS, msmod.mp_arr([101, 100, None]), 11)
    check("返回 2 帧", len(frames3) == 2, len(frames3))
    bag = pstate.get_state().get("item_bag")
    check("数量 1000 -> 900", bag["1"]["num"] == 900, bag["1"]["num"])
    d5 = parse_sync(frames3[1])
    check("同步帧类型 13", d5["type"] == 13, d5["type"])
    check("propertyid = item_bag 的 __id__", d5.get("propertyid") == 1324, d5.get("propertyid"))
    check("name = 格子键 1", d5.get("name") == "1", d5.get("name"))
    check("value 里 num=900", (d5.get("value") or {}).get("num") == 900, d5.get("value"))
    check("value 带 __id__（原地更新要用）", (d5.get("value") or {}).get("__id__") == 1357,
          (d5.get("value") or {}).get("__id__"))

    print("\n[4c] 道具不够时数量夹到 0")
    frames4 = erpc.handle(msmod.RPC_INDEX_USE_ITEM_CS, msmod.mp_arr([101, 99999, None]), 12)
    bag4 = pstate.get_state().get("item_bag")
    check("不会变负数", bag4["1"]["num"] == 0, bag4["1"]["num"])

    print("\n[4d] UpgradeCharacter_CS 升级 + characterBag 子属性同步")
    pstate.reset_state({"characterBag": {"__tp__": "CharacterPersistentDict", "__id__": 1326,
                                         9001: {"__id__": 1341, "useSkinId": 900101}}})
    frames5 = erpc.handle(msmod.RPC_INDEX_UPGRADE_CHARACTER_CS, msmod.mp_arr([9001, {}]), 21)
    check("返回 2 帧", len(frames5) == 2, len(frames5))
    cbag = pstate.get_state().get("characterBag")
    check("CharLevel 默认 1 -> 2", cbag[9001].get("CharLevel") == 2, cbag[9001].get("CharLevel"))
    d6 = parse_sync(frames5[1])
    check("同步帧类型 13", d6["type"] == 13, d6["type"])
    check("propertyid = characterBag 的 __id__", d6.get("propertyid") == 1326, d6.get("propertyid"))
    check("name = 角色 id 字符串", d6.get("name") == "9001", d6.get("name"))
    check("value 里 CharLevel=2", (d6.get("value") or {}).get("CharLevel") == 2,
          (d6.get("value") or {}).get("CharLevel"))

    print("\n[4e] 再升一次到 3")
    erpc.handle(msmod.RPC_INDEX_UPGRADE_CHARACTER_CS, msmod.mp_arr([9001, {}]), 22)
    check("CharLevel = 3", pstate.get_state().get("characterBag")[9001].get("CharLevel") == 3,
          pstate.get_state().get("characterBag")[9001].get("CharLevel"))

    print("\n[4f] 1-3 的 Set* 系列：真正改数据 + 推根属性同步")
    pstate.reset_state({"portrait_id": 0, "is_char_list_shown": False, "canScore": 0,
                        "char_list": {"__tp__": "IntList", "__id__": 1044}})
    fs6 = erpc.handle(erpc.RPC_INDEX["SetPortrait_CS"], msmod.mp_arr([12345]), 31)
    check("SetPortrait 返回 2 帧", len(fs6) == 2, len(fs6))
    check("portrait_id = 12345", pstate.get_state().get("portrait_id") == 12345,
          pstate.get_state().get("portrait_id"))
    d7 = parse_sync(fs6[1])
    check("同步 propertyid = 0（根属性）", d7.get("propertyid") == 0, d7.get("propertyid"))
    check("同步 name = portrait_id", d7.get("name") == "portrait_id", d7.get("name"))
    check("同步 value = 12345", d7.get("value") == 12345, d7.get("value"))

    fs7 = erpc.handle(erpc.RPC_INDEX["SetIsCharListShown_CS"], msmod.mp_arr([True]), 32)
    check("is_char_list_shown = true", pstate.get_state().get("is_char_list_shown") is True,
          pstate.get_state().get("is_char_list_shown"))

    fs8 = erpc.handle(erpc.RPC_INDEX["SetCanScore_CS"], msmod.mp_arr([]), 33)
    check("SetCanScore 无参数也能改", pstate.get_state().get("canScore") == 1,
          pstate.get_state().get("canScore"))

    fs9 = erpc.handle(erpc.RPC_INDEX["SetCharShown_CS"], msmod.mp_arr([[9001, 9002]]), 34)
    cl = pstate.get_state().get("char_list")
    check("char_list 保留 __tp__", cl.get("__tp__") == "IntList", cl.get("__tp__"))
    check("char_list 保留 __id__", cl.get("__id__") == 1044, cl.get("__id__"))
    check("char_list 有 2 项", cl.get("#1") == 9001 and cl.get("#2") == 9002, cl)

    print("\n[5] 其它 RPC 仍然只有一帧")
    frames2 = erpc.handle(erpc.RPC_INDEX["UpgradeCharacter_CS"], b"\x90", 9)
    check("1 帧", len(frames2) == 1, len(frames2))
    print("\n[6] 单向消息返回空列表")
    check("rid=0 -> []", erpc.handle(msmod.RPC_INDEX_SET_SIGNATURE_CS, b"\x90", 0) == [])
    check("index=None -> []", erpc.handle(None, b"\x90", 5) == [])

    print("\n[7] 存档整数键往返（JSON 会把 int 键变字符串，必须用 #N 约定）")
    # load() 有保护：属性少于 10 条视为存档损坏、回退初始值。
    # 所以这里造一份够大的，才能验证往返。
    _props = {1: {"a": 2}, 3: "x"}
    for _i in range(12):
        _props["k%d" % _i] = _i
    # 用独立的 uid，别污染真实玩家存档（load 会优先读盘）
    st = pstate.PlayerState(uid="OO_TEST_SAVE", props=_props)
    st.save()
    st2 = pstate.load(st.uid)
    check("存档能读回", st2 is not None)
    if st2:
        check("整数键 1 仍是 int", 1 in st2.props, list(st2.props.keys()))
        check("整数键 3 仍是 int", 3 in st2.props, list(st2.props.keys()))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
