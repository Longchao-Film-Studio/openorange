#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""传输层回归测试（P0 遗留第 1 条：server.py -> transport/ 拆分之后补的）。

为什么需要：拆分前 transport 层只有人工验证，32 套件 1176 项全绿也**覆盖不到**
handle_gate / handle_ms / handle_capture。审计用 socketpair 做过差分，
但没进仓库 —— 这里把它固化下来。

用 socket.socketpair() 造一对内存 socket，把请求字节写进去，
再把 handler 的输出收集回来按帧解析断言。不占端口、不依赖网络。

覆盖：
  1. gate 绑定链 reqType 0 -> 4 -> 3（ConnectServerResp + bindAccount/Avatar/createChannelEntity）
  2. gate 心跳 type=6 不回包
  3. gate 实体 RPC：networkCheckAfterChange_CS / RpcSyncServerTime 必须回
  4. gate __rpc_index_not_found__ 必须**不回**（审计 P1-1 恢复的防御分支）
  5. gate 粘包：两条帧塞进一次 send 也要都处理
  6. ms：query / batchGetAttribute / 未知方法
  7. capture：--echo 原样回显

用法: python tests/test_transport.py
"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_transport_")
# ★ 抓包目录也要隔离：否则跑一次测试就往 work/captures 里写 conn_*.bin / ms_*.bin
os.environ["OO_CAPTURE_DIR"] = tempfile.mkdtemp(prefix="oo_test_captures_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server.transport import accounts  # noqa: E402
from game_server.transport.capture import handle_capture  # noqa: E402
from game_server.transport.gate import handle_gate  # noqa: E402
from game_server.transport.ms_service import handle_ms  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


ADDR = ("127.0.0.1", 40000)


def run_handler(fn, payloads, wait=0.35, cid=1):
    """把 payloads 依次写进 socketpair，收集 handler 的输出字节。"""
    a, b = socket.socketpair()
    err = []

    def _run():
        try:
            fn(a, ADDR, cid)
        except Exception as e:  # handler 自己的异常不该让测试挂住
            err.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for p in payloads:
        try:
            b.sendall(p)
        except Exception:
            break
        time.sleep(0.05)
    time.sleep(wait)
    b.settimeout(0.4)
    data = b""
    try:
        while True:
            chunk = b.recv(65536)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass
    try:
        b.close()
    except Exception:
        pass
    t.join(timeout=2)
    return data, err


def run_ok(fn, payloads, wait=0.35, cid=1, label=""):
    """run_handler + 断言 handler 没抛异常（审计 H：以前 handler 崩了也会得到 b"" 而 PASS）。"""
    data, err = run_handler(fn, payloads, wait=wait, cid=cid)
    check((label + " handler 没抛异常") if label else "handler 没抛异常", not err,
          err[:1])
    return data, err


def frames_of(data):
    """把字节流切成帧，返回 [(msg_type, body)]。"""
    out = []
    buf = data
    while len(buf) >= 6:
        ln = int.from_bytes(buf[:4], "little")
        if ln < 2 or len(buf) < 4 + ln:
            break
        typ = int.from_bytes(buf[4:6], "little")
        out.append((typ, buf[6:4 + ln]))
        buf = buf[4 + ln:]
    return out


def connect_frame(req_type, device=b"dev", auth=None, version=b"v1"):
    """构造一条 connectServer 帧（type=3）。"""
    body = msmod.pb_varint_field(1, req_type)
    body += msmod.pb_bytes_field(2, device)
    if auth is not None:
        body += msmod.pb_bytes_field(5, auth)
    body += msmod.pb_bytes_field(6, version)
    return msmod.frame(3, body)


def entity_frame(index, params, rid=0, entity_id=1):
    """构造一条实体 RPC 帧（type=4）。"""
    body = msmod.pb_varint_field(3, index)
    body += msmod.pb_bytes_field(4, params)
    if rid:
        body += msmod.pb_varint_field(5, rid)
    return msmod.frame(4, body)


def run():
    print("\n[1] gate 绑定链 reqType=0 -> bindAccount")
    accounts.bind(1, None)
    data, err = run_ok(handle_gate, [connect_frame(0)], cid=1, label="reqType=0")
    fs = frames_of(data)
    types = [t for t, _ in fs]
    check("handler 没抛异常", not err, err[:1])
    check("回了 ConnectServerResp(type=1)", 1 in types, types)
    check("回了 bindAccount(type=6)", 6 in types, types)

    print("\n[1b] gate reqType=0 带 auth（P0-账号绑定：不再靠猜队列）")
    # ★ 注入 Lua 会给 NEW_CONNECTION 的 connectServer 补上
    #   Mailbox{1:"account_<account_id>"}；服务端 _bind_account 优先认 auth，
    #   不再依赖「ms query 时序 + 30 秒窗口」猜队列。
    #   这里 account 名直接给登录名形式，resolve_display_uid 查不到退回原串。
    auth0 = msmod.pb_bytes_field(1, b"account_authbind_u")
    data, _err = run_ok(handle_gate, [connect_frame(0, auth=auth0)], cid=101,
                        label="reqType=0 带 auth")
    fs = frames_of(data)
    types = [t for t, _ in fs]
    check("reqType=0 带 auth 回 ConnectServerResp", 1 in types, types)
    ba = [b for ty, b in fs if ty == 6]
    check("reqType=0 带 auth 回 bindAccount(type=6)", bool(ba), types)
    mb_name = b""
    if ba:
        f = msmod.pb_parse(ba[0])
        mb = msmod.pb_parse(f.get(1, [b""])[0])
        mb_name = mb.get(1, [b""])[0]
    # ★ 关键断言：bindAccount 下发的 mailbox 是 account_authbind_u，
    #   说明服务端从 auth 认出了账号（不是回退 DEFAULT_UID / 猜队列）。
    check("从 auth 认出账号 authbind_u（mailbox 是 account_authbind_u）",
          mb_name == b"account_authbind_u", mb_name)

    print("\n[1c] gate reqType=0 无 auth -> 回退猜队列（老客户端兼容）")
    accounts.bind(102, None)
    data, _err = run_ok(handle_gate, [connect_frame(0)], cid=102,
                        label="reqType=0 无 auth")
    fs = frames_of(data)
    check("无 auth 也回 ConnectServerResp", 1 in [t for t, _ in fs],
          [t for t, _ in fs])

    print("\n[2] gate reqType=4（auth 里带 account_ 名字）-> bindAvatar")
    auth = msmod.pb_bytes_field(1, b"account_transport_u")
    data, _err = run_ok(handle_gate, [connect_frame(4, auth=auth)], cid=2, label="reqType=4")
    fs = frames_of(data)
    types = [t for t, _ in fs]
    check("reqType=4 回 ConnectServerResp", 1 in types, types)
    check("reqType=4 回 bindAvatar(type=7)", 7 in types, types)
    # ★ 连接关闭时 accounts.release() 会摘掉关联，所以不能事后查 uid_of；
    #   改成看下发的 bindAvatar 里 mailbox 名字（它就是用还原出的 uid 拼的）。
    av = [b for ty, b in fs if ty == 7]
    mb_name = b""
    if av:
        f = msmod.pb_parse(av[0])
        mb = msmod.pb_parse(f.get(1, [b""])[0])
        mb_name = mb.get(1, [b""])[0]
    check("从 auth 还原出账号（bindAvatar 的 mailbox 是 avatar_transport_u）",
          mb_name == b"avatar_transport_u", mb_name)

    print("\n[3] gate reqType=3（auth 里带 avatar_ 名字）-> createChannelEntity")
    auth3 = msmod.pb_bytes_field(1, b"avatar_transport_u")
    data, _err = run_ok(handle_gate, [connect_frame(3, auth=auth3)], cid=3, label="reqType=3")
    fs = frames_of(data)
    types = [t for t, _ in fs]
    check("reqType=3 回 createChannelEntity(type=4)", 4 in types, types)
    ch = [b for t, b in fs if t == 4]
    if ch:
        f = msmod.pb_parse(ch[0])
        check("createChannelEntity 带 initDict（field 2 非空）",
              bool(f.get(2, [b""])[0]), len(f.get(2, [b""])[0] or b""))
        check("entityId 是 avatar_transport_u",
              f.get(3, [b""])[0] == b"avatar_transport_u",
              f.get(3, [b""])[0])

    print("\n[4] gate 心跳 type=6 不回包")
    accounts.bind(4, "transport_u")
    hb = msmod.frame(6, msmod.pb_varint_field(1, 1234567890))
    data, _err = run_ok(handle_gate, [hb], cid=4, label="心跳")
    check("心跳不回任何帧", data == b"", data[:20])

    print("\n[5] gate 实体 RPC：networkCheck / timeSync 必须回")
    nc = entity_frame(msmod.RPC_NETWORK_CHECK, msmod.mp_arr([7]), rid=0)
    data, _err = run_ok(handle_gate, [nc], cid=4, label="networkCheck")
    fs = frames_of(data)
    check("networkCheckAfterChange_CS 有应答", len(fs) >= 1, len(fs))
    check("应答是 entityMessage(type=2)", fs and fs[0][0] == 2,
          [t for t, _ in fs])
    ts = entity_frame(msmod.RPC_SYNC_SERVER_TIME, msmod.mp_arr([111]), rid=0)
    data, _err = run_ok(handle_gate, [ts], cid=4, label="timeSync")
    fs = frames_of(data)
    check("RpcSyncServerTime 有应答", len(fs) >= 1, len(fs))

    print("\n[6] gate __rpc_index_not_found__ 必须不回（审计 P1-1）")
    nf = entity_frame(msmod.RPC_INDEX_NOT_FOUND, msmod.mp_arr([1]), rid=99)
    data, _err = run_ok(handle_gate, [nf], cid=4, label="index_not_found")
    check("__rpc_index_not_found__(rid>0) 不回包", data == b"", data[:24])

    print("\n[7] gate 粘包：两条帧一次 send 都要处理")
    two = nc + ts
    data, _err = run_ok(handle_gate, [two], cid=4, label="粘包")
    fs = frames_of(data)
    check("一次 send 两帧 -> 回两帧", len(fs) >= 2, len(fs))

    print("\n[8] ms：query / batchGetAttribute / 未知方法")

    def ms_req(seq, service, method, payload=None):
        body = msmod.pb_varint_field(1, seq)
        body += msmod.pb_bytes_field(2, service.encode())
        body += msmod.pb_bytes_field(3, method.encode())
        if payload is not None:
            body += msmod.pb_bytes_field(4, payload)
        return msmod.frame(3, body)

    q = ms_req(1, "GameLoadService", "query",
               msmod.mp_map({"Username": "transport_ms"}))
    data, _err = run_ok(handle_ms, [q], cid=11, label="ms query")
    fs = frames_of(data)
    check("query 有 MsResponse(type=3)", fs and fs[0][0] == 3,
          [t for t, _ in fs])
    if fs:
        f = msmod.pb_parse(fs[0][1])
        check("MsResponse rid 原样带回", f.get(1, [0])[0] == 1, f.get(1))
        params = msmod.mp_decode(f.get(2, [b""])[0])[0]
        check("query 应答带 ServerList", "ServerList" in params, list(params))

    b = ms_req(2, "UserDataService", "batchGetAttribute",
               msmod.mp_arr([["transport_u", ["level", "signature"]]]))
    data, _err = run_ok(handle_ms, [b], cid=12, label="ms batch")
    fs = frames_of(data)
    if fs:
        f = msmod.pb_parse(fs[0][1])
        params = msmod.mp_decode(f.get(2, [b""])[0])[0]
        check("batchGetAttribute 回 Results",
              isinstance(params.get("Results"), list) and params["Results"],
              params)
    else:
        check("batchGetAttribute 回 Results", False, "没有应答帧")

    u = ms_req(3, "GameLoadService", "noSuchMethod")
    data, _err = run_ok(handle_ms, [u], cid=13, label="ms 未知方法")
    fs = frames_of(data)
    check("未知方法也回 MsResponse（不晾着客户端）", bool(fs), len(fs))

    print("\n[9] capture：--echo 原样回显")
    payload = msmod.frame(3, msmod.pb_varint_field(1, 42))
    data, _err = run_handler(
        lambda c, a, i: handle_capture(c, a, i, echo=True), [payload], cid=21)
    check("echo 模式原样回显", data == payload, (data[:12], payload[:12]))

    print("\n[10] capture：纯抓包模式不回任何东西")
    data, _err = run_handler(
        lambda c, a, i: handle_capture(c, a, i, echo=False), [payload], cid=22)
    check("capture-only 不回包", data == b"", data[:12])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
