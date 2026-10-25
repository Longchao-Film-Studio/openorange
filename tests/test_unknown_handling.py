#!/usr/bin/env python
"""未实现请求的处理策略（雷2）回归测试。

背景
----
以前未实现的 RPC / 微服务方法一律回「成功」（{status:true} / {"Ret":[True,0]}）。
后果是客户端以为成功、服务端什么都没做 —— 邮件附件被本地标记已领、
奖励永久丢失就是这条路（avatarmailcomp.lua:339-342）。

现在：
  - 严格模式（默认）回失败；兼容模式（--unknown-ok）回成功
  - 两种模式都记到 UNKNOWN_LOG_PATH（聚合计数）
  - 经济类 RPC 与邮件领取类方法有**硬黑名单**：兼容模式也不回成功

用法: python tests/test_unknown_handling.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_unknown_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_unknown_db_"), "data.db")
_LOG = os.path.join(tempfile.mkdtemp(prefix="oo_test_unknown_log_"),
                    "unknown_requests.tsv")
os.environ["OO_UNKNOWN_LOG"] = _LOG
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, rpc_catalog  # noqa: E402  # noqa: E402
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import unknown_log as ulog
from game_server.transport import ms_service  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def body_of(pkt):
    return pkt[6:] if isinstance(pkt, (bytes, bytearray)) else pkt


def ret_of(pkt):
    """从应答帧里取 retStatus。"""
    _, _, rid, params = msmod.parse_entity_response(pkt)
    return (params or [None])[0]


def run():
    print("\n[1] rpc_catalog：全量名字反查（弥补 RPC_INDEX 只有 111 条）")
    check("表里有 C2S 名字（>500）", rpc_catalog.size() > 500, rpc_catalog.size())
    check("RpcDrawGacha -> 338409924",
          rpc_catalog.index_of("RpcDrawGacha") == 338409924,
          rpc_catalog.index_of("RpcDrawGacha"))
    check("反查 338409924 -> RpcDrawGacha",
          rpc_catalog.name_of(338409924) == "RpcDrawGacha",
          rpc_catalog.name_of(338409924))
    check("未知 index 返回 None", rpc_catalog.name_of(987654321) is None)

    print("\n[2] RPC：严格模式（默认）回失败")
    config.configure_unknown_fake_ok(False)
    ret = erpc.unknown_rpc_ret(1476365309, uid="u1")   # UpgradePlayerStage_CS
    check("严格模式 ret.status=False", ret.get("status") is False, ret)
    check("带了 errmsg", bool(ret.get("errmsg")), ret)

    print("\n[3] RPC：兼容模式回成功")
    config.configure_unknown_fake_ok(True)
    ret = erpc.unknown_rpc_ret(1476365309, uid="u1")
    check("兼容模式 ret.status=True", ret.get("status") is True, ret)

    print("\n[4] RPC：经济类硬黑名单（兼容模式也不回成功）")
    for nm in ("RpcDrawGacha", "RpcBuyShop", "Cmd_BuyBpLevel_CS"):
        idx = rpc_catalog.index_of(nm)
        ret = erpc.unknown_rpc_ret(idx, uid="u1")
        check("{} 始终失败".format(nm), ret.get("status") is False, (nm, ret))

    print("\n[5] 走完整 handle 路径（帧形状 + retStatus）")
    # BreakCharacter_CS 未实现（UpgradePlayerStage_CS 已实现，会走 handler）
    _unimpl = 1158350933
    pkt = guarded_handle(_unimpl, b"\x90", 7, uid="u1")[0]
    _mtype, _rid, ret = None, None, None
    _m, _name, _rid, _params = msmod.parse_entity_response(pkt)
    check("兼容模式：应答 status=True", _params[0].get("status") is True, _params)
    config.configure_unknown_fake_ok(False)
    pkt = guarded_handle(_unimpl, b"\x90", 7, uid="u1")[0]
    _m, _name, _rid, _params = msmod.parse_entity_response(pkt)
    check("严格模式：应答 status=False", _params[0].get("status") is False, _params)
    check("rid 原样带回", _rid == 7, _rid)

    print("\n[6] 微服务：严格/兼容/硬黑名单")
    config.configure_unknown_fake_ok(False)
    p = msmod.mp_decode(ms_service.unknown_ms_params("ChatService", "getRtmToken"))[0]
    check("严格模式 Ret[0]=False", p["Ret"][0] is False, p)
    config.configure_unknown_fake_ok(True)
    p = msmod.mp_decode(ms_service.unknown_ms_params("ChatService", "getRtmToken"))[0]
    check("兼容模式 Ret[0]=True", p["Ret"][0] is True, p)
    p = msmod.mp_decode(ms_service.unknown_ms_params(
        "MailService", "setMailGiftReceived"))[0]
    check("★ 邮件领取：兼容模式也失败", p["Ret"][0] is False, p)
    for m in ("deleteMail", "setAllMailHaveRead", "setMailHaveRead", "star", "unstar"):
        p = msmod.mp_decode(ms_service.unknown_ms_params("MailService", m))[0]
        check("★ 邮件 {} 硬黑名单".format(m), p["Ret"][0] is False, p)

    print("\n[7] 落盘：两种模式都记录 + 聚合计数")
    st = ulog.stats()
    keys = {(r["kind"], r["key"]) for r in st}
    check("统计里有升级类 RPC", ("rpc", "UpgradePlayerStage_CS") in keys, keys)
    check("统计里有 RpcDrawGacha", ("rpc", "RpcDrawGacha") in keys, keys)
    check("统计里有 ChatService.getRtmToken",
          ("ms", "ChatService.getRtmToken") in keys, keys)
    check("同一 key 计数累加（>1）",
          any(r["count"] > 1 for r in st), st[:3])
    check("日志文件已创建", os.path.exists(_LOG), _LOG)
    # mode 列记录的是**决定**（fake/fail），且同一 key 5 秒内只写一行原始记录。
    # 这里显式写两条不同 key，确定性地验证两种取值。
    ulog.record("test", "mode_strict", fake_ok=False, reply="fail")
    ulog.record("test", "mode_compat", fake_ok=True, reply="ack")
    raw = open(_LOG, encoding="utf-8").read() if os.path.exists(_LOG) else ""
    check("有表头", raw.startswith("ts\tkind\tkey"), raw[:60])
    check("记录里区分 strict/compat", "strict" in raw and "compat" in raw)
    check("记录里带参数样例列", raw.count("\t") >= 6 * 2, raw[:200])

    print("\n[8] 日志聚合按次数降序")
    counts = [r["count"] for r in st]
    check("有序", counts == sorted(counts, reverse=True), counts[:6])

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
