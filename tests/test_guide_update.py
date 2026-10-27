#!/usr/bin/env python
"""H5：System_Guide_Update_CS —— 新手引导进度落盘（以前未实现，只 compat ack）。

客户端依据（avatarsystemguidecomp.lua:371-378）：
  完成一组引导 -> serverRpc({guideId, true}) -> System_Guide_Update_SC(guideId, ret)
  回调只检查 ret.status。服务端语义：把 guideId 记进 system_guide_records
  （IntDictKeyInt，int -> int）；客户端 entryAdded 回调 onChangedGuideRecords
  会把 key 收进 _guideRecords，重登不再重播。

用法: python tests/test_guide_update.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_guide_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    dispatch,  # noqa: E402
    rpc_index,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.ms import initdict  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def parse_sync(pkt):
    fields = msmod.pb_parse(pkt[6:])
    out = {"type": int.from_bytes(pkt[4:6], "little")}
    if 2 in fields:
        out["propertyid"] = fields[2][0]
    if 3 in fields:
        out["optype"] = fields[3][0]
    if 4 in fields:
        out["name"] = fields[4][0].decode("utf-8", "replace")
    if 5 in fields:
        out["value"] = msmod.mp_decode(fields[5][0])[0]
    return out


def response_status(frames):
    for f in frames:
        if isinstance(f, (bytes, bytearray)) and int.from_bytes(f[4:6], "little") == 2:
            try:
                _t, _m, _rid, params = msmod.parse_entity_response(f)
                return params[0].get("status") if isinstance(params, list) and params else None
            except Exception:
                continue
    return None


def run():
    print("\n[1] RPC 已登记")
    check("System_Guide_Update_CS 在 RPC_INDEX",
          "System_Guide_Update_CS" in rpc_index.RPC_INDEX)
    check("索引 = 324175358（客户端 rpc_list）",
          rpc_index.RPC_INDEX.get("System_Guide_Update_CS") == 324175358,
          rpc_index.RPC_INDEX.get("System_Guide_Update_CS"))

    print("\n[2] 上报一组引导 -> 落盘 + 下发一条属性同步")
    initdict.live_init_dict("guide1")
    idx = rpc_index.RPC_INDEX["System_Guide_Update_CS"]
    frames = dispatch.handle(idx, msmod.mp_arr([5, True]), 1, uid="guide1")
    check("有应答帧", response_status(frames) is True, response_status(frames))
    st = pstate.get_state(uid="guide1")
    rec = st.props.get("system_guide_records") or {}
    check("system_guide_records[5] = 1", rec.get(5) == 1, {k: v for k, v in rec.items() if not str(k).startswith("__")})
    syn = []
    for f in frames:
        if isinstance(f, (bytes, bytearray)) and int.from_bytes(f[4:6], "little") == msmod.PROPERTY_SYNC_TYPE:
            try:
                syn.append(parse_sync(f))
            except Exception:
                pass
    check("有 1 条属性同步", len(syn) == 1, len(syn))
    d = syn[0] if syn else {}
    check("propertyid = system_guide_records.__id__",
          d.get("propertyid") == rec.get("__id__"), (d.get("propertyid"), rec.get("__id__")))
    check("name = guideId 字符串", d.get("name") == "5", d.get("name"))
    check("optype = OP_ADD", d.get("optype") == msmod.OP_ADD, d.get("optype"))
    check("value = 1（IntDictKeyInt）", d.get("value") == 1, d.get("value"))

    print("\n[3] 幂等：重复上报同一组不再产生属性帧")
    frames2 = dispatch.handle(idx, msmod.mp_arr([5, True]), 2, uid="guide1")
    syn2 = [f for f in frames2
            if isinstance(f, (bytes, bytearray)) and int.from_bytes(f[4:6], "little") == msmod.PROPERTY_SYNC_TYPE]
    check("第二次只有应答帧", not syn2 and response_status(frames2) is True, len(frames2))
    check("记录没有重复/翻倍", (pstate.get_state(uid="guide1").props["system_guide_records"]).get(5) == 1)

    print("\n[4] 非法参数：回失败而不是崩")
    frames3 = dispatch.handle(idx, msmod.mp_arr([]), 3, uid="guide1")
    check("status = False", response_status(frames3) is False, response_status(frames3))
    frames4 = dispatch.handle(idx, msmod.mp_arr(["not-a-number"]), 4, uid="guide1")
    check("非数字 guideId -> status = False", response_status(frames4) is False, response_status(frames4))

    print("\n[5] E2E：system_guide_records 结构交给客户端 Lua 解析")
    try:
        from tests.lua_harness import LUA_ROOT, boot_config, new_runtime, parse_as_client

        if not os.path.isdir(LUA_ROOT):
            raise ImportError("没有客户端 Lua 目录")
        lua = new_runtime()
        boot_config(lua)
        ok, err = parse_as_client(lua, "IntDictKeyInt", {"__id__": 7777, 5: 1})
        check("解析通过", ok, err)
    except ImportError as e:
        print(f"  (跳过 E2E：{e})")

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
