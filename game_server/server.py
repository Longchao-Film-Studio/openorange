#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.server —— 游戏服（微服务网关 + 游戏网关）

监听:
    33100  微服务网关（ClientGate.syncMetadata / GameLoadService.query）
    33101  游戏网关（connectServer / connectResponse / bindAccount / bindAvatar）

常用模式:
  ★ 正式联调（推荐）
      python -m game_server.server --ms-ok
      33100 回正确的 MsResponse，33101 回 ConnectServerResp，
      并按 reqType 0 -> 4 -> 3 推进绑定链（bindAccount -> bindAvatar）。

  纯抓包（看客户端发什么，什么都不回）
      python -m game_server.server

  反代抓包（把流量原样转发给官方网关，观察官方应答）
      python -m game_server.server --proxy <官方网关 host:port>

  回显
      python -m game_server.server --echo

记录内容:
  * 连接建立/断开（客户端地址、持续时间、上游地址）
  * 每次 recv 的：相对时间(ms)、方向(C=客户端发来 / S=服务端发来)、字节数、十六进制、ASCII
  * 原始字节流另存到 work/captures/conn_*.bin
"""
import os
import socket
import struct
import sys
import threading
import time

from . import ms as msmod
from . import entity_rpc as erpc

try:
    from shared import db as shared_db
except Exception:
    shared_db = None

# 最近一次 ms GameLoadService.query 里带的账号名。
# gate 的帧里没有账号标识，但同一个客户端会先走 ms 登录，
# 所以在这里记下来。
_last_username = None

# ---- 多账号：gate 连接 <-> 账号 的关联 ----
#
# 客户端登录顺序是固定的：ms 上先发 GameLoadService.query（带 Username），
# 然后 gate 建新连接（reqType=0）。所以用「待关联队列 + 时间窗」把它们对上。
# 局限：两个客户端**同时**启动时可能串号（先后启动没问题）。
# 要彻底解决得让客户端把账号带进 connectServer 的 auth 字段。
_USERNAME_LOCK = threading.Lock()
_pending_usernames = []          # [(ts, username)]
_gate_uid = {}                   # cid -> username（= 游戏内 uid）
USERNAME_MATCH_WINDOW = 30       # 秒


def _resolve_display_uid(raw):
    """把 ms query 里的 Username 还原成玩家看得懂的 uid。

    ms GameLoadService.query 里的 Username 其实是 **account_id**
    （`oo_` + sha256("acct:"+登录名) 前 16 位），例如
    `oo_08bed76013b06783` 对应登录名 `15900159000`。
    所以查库换回真正的登录名；查不到就用账号表自增 id（1、2、3…）兜底，
    再不行才用原始串。
    """
    if not raw:
        return None
    if shared_db is not None:
        try:
            acc = shared_db.get_account_by_account_id(raw)
            if acc:
                name = (acc.get("username") or "").strip()
                if name:
                    return name
                print("[GATE] 警告：account_id=%s 的账号没有登录名，只能退回原始串"
                      % raw, flush=True)
            else:
                print("[GATE] 警告：account_id=%s 在库里查不到，只能退回原始串" % raw,
                      flush=True)
        except Exception as _e:
            print("[GATE] 警告：还原登录名失败 %s" % _e, flush=True)
    # ★ 不用数字兜底：uid 必须是登录名。查不到才退回原始串（正常不会走到）。
    return raw


def _remember_username(username):
    """ms query 里看到的账号，还原成 uid 后放进待关联队列。"""
    if not username:
        return
    username = _resolve_display_uid(username)
    if not username:
        return
    now = time.time()
    with _USERNAME_LOCK:
        _pending_usernames.append((now, username))
        # 只留窗口内的，防止无限增长
        while _pending_usernames and now - _pending_usernames[0][0] > USERNAME_MATCH_WINDOW * 4:
            _pending_usernames.pop(0)


def _claim_username():
    """给一条新 gate 连接领一个账号名（最早的、窗口内的）。领不到返回 None。"""
    now = time.time()
    with _USERNAME_LOCK:
        while _pending_usernames:
            ts, name = _pending_usernames.pop(0)
            if now - ts <= USERNAME_MATCH_WINDOW:
                return name
        return None


def _uid_of(cid):
    """这条 gate 连接对应的 uid（= 账号名）。没有就回退默认值。"""
    return _gate_uid.get(cid) or "OO00000001"


def _uid_from_auth(fields):
    """从 connectServer 的 auth 字段里还原账号 uid。

    绑定链的每一步都是**新连接**（新 cid），所以账号不能只靠 cid 记住。
    好在 bindAccount/bindAvatar 下发的 mailbox 名字客户端会原样回传在 auth 里：
      reqType=4 -> auth = Mailbox{1:"account_<uid>", 2:ip, 3:port, 4:...}
      reqType=3 -> auth = Mailbox{1:"avatar_<uid>",  ...}
    所以剥掉前缀就能把账号带过整条链路。
    """
    raw = fields.get(5)
    if not raw:
        return None
    try:
        mb = msmod.pb_parse(raw[0])
        name = mb.get(1, [b""])[0]
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        for p in ("account_", "avatar_"):
            if name.startswith(p):
                return name[len(p):]
    except Exception:
        pass
    return None

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db  # noqa: E402

from . import config as ooconf  # noqa: E402

# ★ 端口/路径一律从 game_server.config 取，不要在这里写死。
HOST = ooconf.HOST
PORT = ooconf.MS_PORT

_ROOT = ooconf.ROOT_DIR
CAPTURE_DIR = ooconf.CAPTURE_DIR

ECHO = False
UPSTREAM = None          # (host, port)

# 应答变体轮换：用于爆破 connectResponse 的消息类型号。
# 每个连接按序号取一个变体，客户端每 8 秒自动重连一次，
# 于是一次运行就能把所有候选试完。
# 格式: (u16 消息类型, protobuf body 的十六进制)
# ---- 微服务模式 ----
# 33100 跑 ms 服务（GameLoadService），33101 跑网关（仅用于探测"应答是否被接受"）。
# 成功判据：客户端收到正确的 ms 应答后，会去连 ServerList 里 clusters.addrs 指定的
# 网关地址，也就是 33101 —— 只要那里出现连接，就证明应答格式对了。
MS_MODE = False
MS_PORT = ooconf.MS_PORT
GATE_PORT = ooconf.GATE_PORT
MS_BRUTE = False       # 全量爆破类型号
MS_BRUTE_MAX = 256
# 语义探针：始终回 ERR_CONN_NEED_BIND。
# 若客户端收到后改发 ClientGate.bind，说明应答的"类型号+字段布局"是对的，
# 只是语义不对 —— 这样就能把类型号锁定。
MS_BIND_PROBE = False
# 网关应答模式：对网关连接回正确的 ConnectServerResp
GATE_OK = False
# 微服务正确应答模式：回 MsResponse{rid, params(msgpack), service_id}
MS_OK = False
_ms_seq = 0
_ms_type_cursor = 0
_ms_lock = threading.Lock()

RESPOND_CYCLE = False
# 假设：应答是"RPC 型"消息，需要在类型号之后带上方法名哈希。
# genRpcIndex("connectResponse") = 2119322371 = 0x7E524B03，小端 = 03 4B 52 7E。
# body 用 protobuf{1: 1} = 08 01（respType = CONNECTED）。
# 只试 6 个类型号 —— 实测 type 0..14 不会崩溃，15 及以上会让 C# 解析器越界。
_CR_HASH_LE = "034b527e"
RESPOND_VARIANTS = [(t, _CR_HASH_LE + "0801") for t in (4, 3, 2, 5, 0, 1)]


def _build_response(idx):
    """按序号构造应答字节：[u32 长度][u16 类型][protobuf body]"""
    typ, body_hex = RESPOND_VARIANTS[idx % len(RESPOND_VARIANTS)]
    body = bytes.fromhex(body_hex)
    payload = typ.to_bytes(2, "little") + body
    return len(payload).to_bytes(4, "little") + payload, typ, body_hex
_conn_seq = 0
_seq_lock = threading.Lock()


def _next_id():
    global _conn_seq
    with _seq_lock:
        _conn_seq += 1
        return _conn_seq


def _hexdump(data, width=16, limit=None):
    n = len(data) if limit is None else min(limit, len(data))
    out = []
    for off in range(0, n, width):
        chunk = data[off:off + width]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("      %04X  %-*s  |%s|" % (off, width * 3, hx, asc))
    if limit is not None and len(data) > limit:
        out.append("      ... (共 %d 字节，其余见 .bin)" % len(data))
    return "\n".join(out)


def _log_packet(cid, direction, data, t0, f):
    dt = int((time.time() - t0) * 1000)
    print("[game] #%03d %s +%dms  %d B" % (cid, direction, dt, len(data)), flush=True)
    print(_hexdump(data, limit=512), flush=True)
    if f is not None:
        try:
            f.write(("%s %d %d\n" % (direction, dt, len(data))).encode())
            f.write(data)
            f.flush()
        except Exception:
            pass


def _pump(src, dst, direction, cid, t0, f, done_evt):
    """单向转发 + 记录。"""
    try:
        while True:
            try:
                data = src.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            _log_packet(cid, direction, data, t0, f)
            try:
                dst.sendall(data)
            except OSError as e:
                print("[game] #%03d %s forward failed: %s" % (cid, direction, e), flush=True)
                break
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        done_evt.set()


def _next_ms_seq():
    global _ms_seq
    with _ms_lock:
        _ms_seq += 1
        return _ms_seq


def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _handle_ms(conn, addr, cid):
    """微服务连接：解析请求帧并回候选应答。"""
    t0 = time.time()
    cap_path = os.path.join(CAPTURE_DIR, "ms_%03d_%s.bin" % (cid, addr[1]))
    print("[ms] #%03d OPEN from %s:%d" % (cid, addr[0], addr[1]), flush=True)
    f = None
    try:
        f = open(cap_path, "wb")
    except Exception:
        pass

    try:
        conn.settimeout(60)
        while True:
            hdr = _recv_exact(conn, 4)
            if hdr is None:
                print("[ms] #%03d peer closed" % cid, flush=True)
                break
            ln = struct.unpack("<I", hdr)[0]
            if ln <= 0 or ln > 1 << 20:
                print("[ms] #%03d bad length %d" % (cid, ln), flush=True)
                break
            payload = _recv_exact(conn, ln)
            if payload is None:
                print("[ms] #%03d truncated payload" % cid, flush=True)
                break

            mtype = struct.unpack("<H", payload[:2])[0]
            body = payload[2:]
            if f is not None:
                try:
                    f.write(hdr + payload)
                    f.flush()
                except Exception:
                    pass

            try:
                fields = msmod.pb_parse(body)
            except Exception as e:
                print("[ms] #%03d protobuf parse failed: %s" % (cid, e), flush=True)
                print(_hexdump(payload, limit=256), flush=True)
                continue

            seq = fields.get(1, [0])[0]
            service = fields.get(2, [b""])[0]
            method = fields.get(3, [b""])[0]
            try:
                service = service.decode("utf-8", "replace")
                method = method.decode("utf-8", "replace")
            except Exception:
                pass

            print("[ms] #%03d RX type=%d seq=%s service=%s method=%s  (%d B)"
                  % (cid, mtype, seq, service, method, len(payload)), flush=True)
            print(_hexdump(payload, limit=256), flush=True)

            # 按"请求"轮换类型号（而不是按连接），
            # 这样客户端的重试风暴会自动把全部类型号试一遍。
            # ms 层被拒绝时客户端只会重试，不会崩溃，因此全量爆破是安全的。
            if MS_OK:
                # ★ 正确应答：MsResponse {1: rid, 2: msgpack params, 4: service_id}
                #   type 号 3 来自 EnumG2C 顺序（ResponseEncryptToken/ConfirmEncryptKeyAck/MicroResponse/...）
                #
                # 注意：按方法名区分负载。
                #   syncMetadata 的 Lua 回调 onSyncMetadataCb 只读 retStatus，
                #     给它塞 ServerList 属于多余数据，可能引起解析侧异常；
                #   query 的 onGetServerGates 才需要 ServerList + ClusterId。
                if method == "query":
                    # 从 query 参数里抓账号名（field 4 是 msgpack，含 Username）
                    global _last_username
                    if 4 in fields:
                        try:
                            _q = msmod.mp_decode(fields[4][0])[0]
                            if isinstance(_q, dict) and _q.get("Username"):
                                _last_username = _q["Username"]
                                _remember_username(_last_username)
                                print("[ms] #%03d 账号 %s" % (cid, _last_username), flush=True)
                        except Exception:
                            pass
                    params = msmod.build_load_service_params(GATE_PORT)
                elif method == "batchGetAttribute":
                    # ★ 1-3 玩家数据的主通道（avataruserdatacomp.lua:151）
                    _req = []
                    if 4 in fields:
                        try:
                            _req = msmod.mp_decode(fields[4][0])[0]
                        except Exception as _e:
                            print("[ms] #%03d batchGetAttribute 参数解析失败: %s"
                                  % (cid, _e), flush=True)
                            _req = []
                    params = msmod.build_userdata_batch_params(_req)
                    print("[ms] #%03d    -> 回 %d 条 Results"
                          % (cid, len(msmod.mp_decode(params)[0].get("Results", []))),
                          flush=True)
                else:
                    params = msmod.mp_map({"Ret": [True, 0]})
                pkt = msmod.frame_ms_response(seq, params, service)
                try:
                    conn.sendall(pkt)
                    print("[ms] #%03d TX MsResponse type=3 rid=%s service=%s method=%s (%d B)"
                          % (cid, seq, service, method, len(pkt)), flush=True)
                except Exception as e:
                    print("[ms] #%03d send failed: %s" % (cid, e), flush=True)
                continue

            if MS_BRUTE:
                global _ms_type_cursor
                with _ms_lock:
                    typ = _ms_type_cursor % MS_BRUTE_MAX
                    _ms_type_cursor += 1
                rtype = typ
                if MS_BIND_PROBE:
                    rbody = msmod.build_by_type(typ, seq, GATE_PORT,
                                                ret_status=False,
                                                ret_err="ERR_CONN_NEED_BIND")
                    name = "bindprobe-t%d" % typ
                else:
                    rbody = msmod.build_by_type(typ, seq, GATE_PORT)
                    name = "brute-t%d" % typ
            else:
                cands = msmod.build_candidates(seq, GATE_PORT)
                idx = (cid - 1) % len(cands)
                name, rtype, rbody = cands[idx]
            pkt = msmod.frame(rtype, rbody)
            try:
                conn.sendall(pkt)
                print("[ms] #%03d TX %s type=%d  (%d B)"
                      % (cid, name, rtype, len(pkt)), flush=True)
            except Exception as e:
                print("[ms] #%03d send failed: %s" % (cid, e), flush=True)
                break
    except Exception as e:
        print("[ms] #%03d error: %s" % (cid, e), flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        print("[ms] #%03d CLOSE after %dms" % (cid, int((time.time() - t0) * 1000)), flush=True)


# 网关应答变体：看哪个能让连接活过 4 秒
_bind_push_count = 0

GATE_VARIANTS = [
    ("无应答", None),
    ("t1 08 01 + sid", lambda: msmod.frame_connect_resp(1, "OO00000001")),
    ("t1 08 01 无sid", lambda: msmod.frame_connect_resp(1, "")),
    ("t1 08 03 Reconnected + sid", lambda: msmod.frame_connect_resp(3, "OO00000001")),
    ("t1 08 01 + 长sid", lambda: msmod.frame_connect_resp(1, "session_oo_08bed76013b06783_0001")),
    ("t1 08 01 + sid + content_type0", lambda: msmod.frame(
        1, msmod.pb_varint_field(1, 1) + msmod.pb_bytes_field(2, b"OO00000001")
           + msmod.pb_varint_field(5, 0))),
]
_gate_var_cursor = 0


def _handle_gate_probe(conn, addr, cid):
    """游戏网关：接受 connectServer，回 ConnectServerResp(Connected)。

    应答格式已从二进制里的 client_gate.proto 描述符完整解出：
        message ConnectServerResp { RespType type = 1; string sessionId = 2; }
        enum RespType { Busy=0; Connected=1; ConnectRefused=2; Reconnected=3; ... }
    消息类型号 1（IGateClientService 方法表第一项）。
    """
    t0 = time.time()
    print("", flush=True)
    print("=" * 64, flush=True)
    print("[GATE] ★ 收到网关连接 %s:%d" % (addr[0], addr[1]), flush=True)
    print("=" * 64, flush=True)
    try:
        conn.settimeout(60)
        _buf = bytearray()

        def _next_frame():
            """按 [u32 LE payloadLen][u16 LE msgType][payload] 切出**一帧**。

            以前是把一次 recv 的数据整块当一帧处理。实测客户端会把两条实体
            消息粘在同一个 TCP 段里发（77 B = Cmd_Avatar_StartGame_CS + 心跳），
            于是第二条被 protobuf 解析失败直接丢掉——丢的正好是
            networkCheckAfterChange_CS(index=0)，客户端 _recvHeartbeatIndex
            就停在 0，差值越拉越大，最后触发弱网重连。
            现在按帧头长度切，一次 recv 里有多少帧就处理多少帧。
            """
            nonlocal _buf
            while True:
                frames, rest = msmod.split_frames(_buf)
                if frames:
                    _buf[:] = rest
                    return frames[0]
                chunk = conn.recv(8192)
                if not chunk:
                    return b""
                _buf += chunk

        while True:
            data = _next_frame()
            if not data:
                print("[GATE] #%03d peer closed" % cid, flush=True)
                break
            print("[GATE] #%03d RX %d B:" % (cid, len(data)), flush=True)
            print(_hexdump(data, limit=512), flush=True)
            if len(data) >= 6:
                frame_type = struct.unpack_from("<H", data, 4)[0]
                # ★ 真正的 reqType 在 protobuf 字段 1 里（08 XX），不是帧头
                proto_req = None
                try:
                    body = data[6:]
                    fields = msmod.pb_parse(body)
                    if 1 in fields and isinstance(fields[1][0], int):
                        proto_req = fields[1][0]
                    dev = fields.get(2, [b""])[0]
                    ver = fields.get(6, [b""])[0]
                    print("[GATE] #%03d 帧类型=%d  proto.reqType=%s  deviceId=%s  version=%s"
                          % (cid, frame_type, proto_req,
                             dev.decode("utf-8", "replace") if isinstance(dev, bytes) else dev,
                             (ver.decode("utf-8", "replace")[:20] if isinstance(ver, bytes) else ver)),
                          flush=True)
                except Exception as e:
                    print("[GATE] #%03d 帧类型=%d  protobuf 解析失败: %s" % (cid, frame_type, e), flush=True)

            # ★ 多账号：新连接（reqType=0）到达时，从待关联队列里领一个账号名。
            #   必须在回 ConnectServerResp 之前领，后面整条链路都要用这个 uid。
            if proto_req == 0 and cid not in _gate_uid:
                _claimed = _claim_username()
                _gate_uid[cid] = _claimed
                print("[GATE] #%03d 关联账号 %s"
                      % (cid, _claimed or "(队列为空，回退 OO00000001)"), flush=True)
            elif proto_req in (2, 3, 4) and cid not in _gate_uid:
                # 绑定链的后续连接：从 auth 里的 mailbox 名字还原账号
                try:
                    _u = _uid_from_auth(fields)
                except Exception:
                    _u = None
                if _u:
                    _gate_uid[cid] = _u
                    print("[GATE] #%03d 从 auth 还原账号 %s" % (cid, _u), flush=True)

            # 正确应答：ConnectServerResp {1: Connected, 2: sessionId, 5: content_type=MSGPACK}
            # content_type 必须显式写出（proto2 has-field 语义），否则客户端判为无效。
            #
            # ★ 2026-10-19：只对「连接建立」那一帧回 ConnectServerResp。
            #   实体创建成功之后，客户端会在同一条连接上继续发实体 RPC（帧类型 4，
            #   字段 3 = entityId 哈希、字段 4 = msgpack 参数）。对这些帧再回
            #   ConnectServerResp 是错的，只会让客户端困惑。
            #   实测：reqType=3 那条连接在 createChannelEntity 之后会连发 5 帧
            #   0C 00 00 00 | 04 00 | 18 <varint> | 22 <msgpack>
            # ★ 2026-10-19 修：判断「连接帧」不能只看 field 1 存在。
            #   实机抓到 type=6 的帧：
            #     0A 00 00 00 06 00 08 C8 92 C8 EB AA AE 21
            #   field1 = 纳秒时间戳，那是 sendHeartbeat(Time.getNanosecond())
            #   （core/client/gateclientrpchandler.lua:255），不是 reqType。
            #   以前把它当连接帧回 ConnectServerResp，客户端收到莫名的
            #   Connected 就会断开重连，表现为「进街区后又掉线、反复重登」。
            #   真正的连接帧是 type=3（sendConnectServer）。
            is_heartbeat_frame = (frame_type == 6)
            is_connect_frame = (frame_type == 3 and proto_req is not None)
            if is_heartbeat_frame:
                # sendHeartbeat 是单向的，不回。心跳同时用来刷新在线表的 last_seen_ts。
                if shared_db is not None:
                    try:
                        shared_db.touch_game_online("gate-%03d" % cid)
                    except Exception:
                        pass
                print("[GATE] #%03d 心跳 type=6，不回" % cid, flush=True)
            elif is_connect_frame:
                _sid = _uid_of(cid)
                pkt = msmod.frame_connect_resp(1, _sid)
                conn.sendall(pkt)
                print("[GATE] #%03d TX ConnectServerResp {1:Connected, 2:%s, 5:MSGPACK} (%d B)"
                      % (cid, _sid, len(pkt)), flush=True)
                print(_hexdump(pkt, limit=256), flush=True)
            else:
                # 实体 RPC：试探性地 echo field 3 回去，参数给空 msgpack map。
                # 目的只是看客户端会不会因为「收到应答」而继续往下走。
                idx, params, rid = msmod.parse_entity_frame(data[6:])
                print("[GATE] #%03d 实体消息 type=%d index=%s rid=%s params=%s"
                      % (cid, frame_type, idx, rid,
                         params.hex() if isinstance(params, bytes) else params),
                      flush=True)
                if idx is not None:
                    rpkt = None
                    label = None
                    if idx == msmod.RPC_SYNC_SERVER_TIME:
                        # 客户端发 [客户端时间]，回 {ct, st}
                        ct = 0
                        try:
                            val, _ = msmod.mp_decode(params or b"")
                            if isinstance(val, list) and val:
                                ct = int(val[0])
                        except Exception as e:
                            print("[GATE] #%03d 解析客户端时间失败: %s" % (cid, e), flush=True)
                        rpkt = msmod.build_time_sync(ct, int(time.time()))
                        label = "RpcOnSyncServerTime(ct=%d)" % ct
                    elif idx == msmod.RPC_NETWORK_CHECK:
                        # networkCheckAfterChange_CS：客户端心跳，必须回
                        # networkCheckAfterChange_SC(index)，否则 15 次后断线重连
                        hb = 0
                        try:
                            val, _ = msmod.mp_decode(params or b"")
                            if isinstance(val, list) and val:
                                hb = int(val[0])
                        except Exception as e:
                            print("[GATE] #%03d 解析心跳序号失败: %s" % (cid, e), flush=True)
                        rpkt = msmod.build_net_check_ack(hb)
                        label = "networkCheckAfterChange_SC(%d)" % hb
                    elif idx == msmod.RPC_CMD_AVATAR_STARTGAME_CS:
                        # clientavatar.lua:180 序章 StartGame。已经把
                        # story_finished[1] 置真，正常不会走到这里。
                        print("[GATE] #%03d ★ 客户端仍在请求序章 StartGame "
                              "(rid=%s)，说明 story_finished[1] 没生效" % (cid, rid),
                              flush=True)
                        label = None
                    elif idx == msmod.RPC_INDEX_NOT_FOUND:
                        # __rpc_index_not_found__：客户端在说「你发了个我不认识的 rpc」
                        # 这是我们自己 echo 造成的，现在不再 echo，理论上不会再出现
                        label = None
                    else:
                        # 其余全部交给实体 RPC 层：带 rid 的一律回
                        # entityMessageResponse，单向的返回 None。
                        # 阶段 1 三个组件的 RPC 清单在 game_server/entity_rpc.py。
                        rpkt = erpc.handle(idx, params, rid, uid=_uid_of(cid))
                        if rpkt is not None:
                            label = "%s(rid=%d)" % (erpc.describe(idx), rid)

                    # ★ rpkt 有两种形态：
                    #   - 裸 bytes：老路径（如 networkCheckAfterChange_SC）；
                    #   - 帧列表：entity_rpc.handle 返回（应答帧 + 可能的属性同步帧）。
                    # 之前直接 for 遍历，裸 bytes 会被逐字节当成 int 发出去，
                    # 报 "a bytes-like object is required, not 'int'" 并断连。
                    if isinstance(rpkt, (bytes, bytearray)):
                        _pkts = [rpkt]
                    elif rpkt:
                        _pkts = list(rpkt)
                    else:
                        _pkts = []
                    for _pkt in _pkts:
                        conn.sendall(_pkt)
                        print("[GATE] #%03d TX %s (%d B)"
                              % (cid, label, len(_pkt)), flush=True)
                        print(_hexdump(_pkt, limit=256), flush=True)
                    if len(_pkts) > 1:
                        print("[GATE] #%03d TX %s + %d 条属性同步"
                              % (cid, label, len(_pkts) - 1), flush=True)
                    else:
                        print("[GATE] #%03d %s 无需应答" % (cid, erpc.describe(idx)), flush=True)

            # 主动下发 bindAccount —— 只在 reqType=0（NEW_CONNECTION）时发。
            #
            # 实测：
            #   reqType=0 + 下发 bindAccount -> 客户端接受，随即用 reqType=4(BIND_ACCOUNT) 重连
            #   reqType=4 + 再下发 bindAccount -> 客户端又重连，形成死循环
            # 所以 BIND_ACCOUNT 连接只回 connectResponse 即可。
            # 按 reqType 推进绑定链：
            #   reqType=0 NEW_CONNECTION  -> bindAccount  (客户端回 reqType=4)
            #   reqType=4 BIND_ACCOUNT    -> bindAvatar   (客户端回 reqType=3)
            #   reqType=3 BIND_AVATAR     -> bindSoul     (客户端回 reqType=2)
            #   reqType=2 BIND_SOUL       -> 绑定完成
            global _bind_push_count
            # ★ 2026-10-19 修：这里原来是 `_bind_push_count <= 12` 的调试限制，
            #   每帧 +1，只够走 4 轮完整绑定链。之后任何新连接都拿不到
            #   bindAccount/bindAvatar/createChannelEntity，客户端只能无限重连
            #   （实机表现：第一次能进街区，之后一直转圈重登）。
            #   真实服务端对每条连接都必须应答，这里改成无条件。
            _bind_push_count += 1
            if True:
                _sid = _uid_of(cid)
                if proto_req == 0:
                    # mailbox 名字带上账号，客户端会把 auth 原样回传，
                    # 便于在日志里确认「这条连接确实是这个账号」
                    bpkt = msmod.frame_bind_account(
                        session_id=_sid,
                        act_mailbox=msmod.build_entity_mailbox(
                            entity_id="account_%s" % _sid))
                    label = "bindAccount"
                elif proto_req == 4:
                    bpkt = msmod.frame_bind_avatar(
                        session_id=_sid,
                        avt_mailbox=msmod.build_entity_mailbox(
                            entity_id="avatar_%s" % _sid))
                    label = "bindAvatar"
                elif proto_req == 3:
                    # ★ reqType=3 (BIND_AVATAR)：客户端在这里等 createChannelEntity。
                    #
                    # 实测证据（work/oo_analysis/gamesrv_run.log）：
                    #   reqType=0 -> 发 bindAccount -> 客户端 150ms 后主动断开重连（正常）
                    #   reqType=4 -> 发 bindAvatar  -> 客户端  16ms 后主动断开重连（正常）
                    #   reqType=3 -> 什么都不发    -> 客户端 3700ms 后超时断开重连（卡点）
                    # 3.7 秒正好是客户端的连接超时，说明它在等一条消息。
                    #
                    # 依据 gateclientrpchandler.lua:createChannelEntity，
                    # 这条消息是 IGateClientService 的第 4 个方法。
                    _sid = _uid_of(cid)
                    bpkt = msmod.build_channel_entity(
                        entity_id="avatar_%s" % _sid,
                        session_id=_sid,
                        uid=_sid)
                    label = "createChannelEntity"
                else:
                    bpkt = None
                    label = None
                if bpkt is not None:
                    conn.sendall(bpkt)
                    print("[GATE] #%03d TX %s  (reqType=%d) (%d B)"
                          % (cid, label, proto_req, len(bpkt)), flush=True)
                    print(_hexdump(bpkt, limit=256), flush=True)
                    # ★ 上报游戏内在线：createChannelEntity 一发出去，
                    #   客户端就已经在实体层了（onBecomePlayer 随后触发）。
                    if label == "createChannelEntity" and shared_db is not None:
                        try:
                            # username 用解析后的 uid（登录名），不是 ms query 里的
                            # 原始 account_id（oo_+sha256）
                            _uid = _uid_of(cid)
                            shared_db.set_game_online(
                                session_id="gate-%03d" % cid,
                                username=_uid,
                                uid=_uid,
                                entity_id=ooconf.avatar_entity_id(_uid),
                                addr="%s:%d" % (addr[0], addr[1]))
                            print("[GATE] #%03d 在线登记 %s" % (cid, _uid), flush=True)
                        except Exception as _e:
                            print("[GATE] #%03d 在线登记失败: %s" % (cid, _e), flush=True)
    except Exception as e:
        print("[GATE] #%03d error: %s" % (cid, e), flush=True)
    finally:
        dur = int((time.time() - t0) * 1000)
        # 连接断开就从在线表里摘掉，并释放账号关联
        _gate_uid.pop(cid, None)
        if shared_db is not None:
            try:
                shared_db.clear_game_online("gate-%03d" % cid)
            except Exception:
                pass
        print("[GATE] #%03d CLOSE after %d ms  %s"
              % (cid, dur, "★★ 活过 4 秒！" if dur > 5000 else ""), flush=True)
        try:
            conn.close()
        except Exception:
            pass


def _handle(conn, addr, cid):
    t0 = time.time()
    cap_path = os.path.join(CAPTURE_DIR, "conn_%03d_%s.bin" % (cid, addr[1]))
    print("[game] #%03d OPEN  from %s:%d" % (cid, addr[0], addr[1]), flush=True)

    f = None
    try:
        f = open(cap_path, "wb")
    except Exception as e:
        print("[game] #%03d capture file error: %s" % (cid, e), flush=True)

    try:
        conn.settimeout(120)

        if UPSTREAM is not None:
            up_host, up_port = UPSTREAM
            print("[game] #%03d upstream -> %s:%d" % (cid, up_host, up_port), flush=True)
            up = socket.create_connection((up_host, up_port), timeout=10)
            up.settimeout(120)
            e1, e2 = threading.Event(), threading.Event()
            t1 = threading.Thread(target=_pump, args=(conn, up, "C>", cid, t0, f, e1), daemon=True)
            t2 = threading.Thread(target=_pump, args=(up, conn, "S<", cid, t0, f, e2), daemon=True)
            t1.start(); t2.start()
            t1.join(timeout=300); t2.join(timeout=300)
            try:
                up.close()
            except Exception:
                pass
            return

        # 纯观察 / 回显
        while True:
            try:
                data = conn.recv(8192)
            except socket.timeout:
                print("[game] #%03d recv timeout (idle 120s), closing" % cid, flush=True)
                break
            except ConnectionResetError:
                print("[game] #%03d reset by peer" % cid, flush=True)
                break
            if not data:
                print("[game] #%03d peer closed" % cid, flush=True)
                break
            _log_packet(cid, "RX", data, t0, f)
            if MS_OK:
                # 正确应答：MsResponse，type=3，params 是 msgpack
                params = msmod.build_load_service_params(GATE_PORT)
                pkt = msmod.frame_ms_response(seq, params, service)
                try:
                    conn.sendall(pkt)
                    print("[ms] #%03d TX MsResponse type=3 rid=%s service_id=%s (%d B)"
                          % (cid, seq, service, len(pkt)), flush=True)
                except Exception as e:
                    print("[ms] #%03d send failed: %s" % (cid, e), flush=True)
            if GATE_OK:
                # 解析请求类型号，只对 connectServer(3) 回 ConnectServerResp
                if len(data) >= 6:
                    req_type = struct.unpack_from("<H", data, 4)[0]
                    print("[gate] #%03d 请求类型号 = %d" % (cid, req_type), flush=True)
                pkt = msmod.frame_connect_resp(1, "OO00000001")
                try:
                    conn.sendall(pkt)
                    print("[gate] #%03d TX ConnectServerResp type=1 sessionId=OO00000001 (%d B)"
                          % (cid, len(pkt)), flush=True)
                    print(_hexdump(pkt, limit=256), flush=True)
                except Exception as e:
                    print("[gate] #%03d 发送失败: %s" % (cid, e), flush=True)
            if RESPOND_CYCLE:
                pkt, typ, bh = _build_response(cid - 1)
                try:
                    conn.sendall(pkt)
                    print("[game] #%03d TX variant=%d type=%d body=%s  (%d B)"
                          % (cid, (cid - 1) % len(RESPOND_VARIANTS), typ, bh or "(空)", len(pkt)),
                          flush=True)
                    print(_hexdump(pkt, limit=256), flush=True)
                except Exception as e:
                    print("[game] #%03d respond failed: %s" % (cid, e), flush=True)
            if ECHO:
                try:
                    conn.sendall(data)
                except Exception as e:
                    print("[game] #%03d echo failed: %s" % (cid, e), flush=True)
    except Exception as e:
        print("[game] #%03d error: %s" % (cid, e), flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        dur = int((time.time() - t0) * 1000)
        print("[game] #%03d CLOSE after %dms -> %s"
              % (cid, dur, os.path.basename(cap_path)), flush=True)


def serve(host=None, port=None):
    db.init()
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    host = host or HOST
    port = port or PORT
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    if UPSTREAM:
        mode = "proxy->%s:%d" % UPSTREAM
    elif RESPOND_CYCLE:
        mode = "respond-cycle(%d variants)" % len(RESPOND_VARIANTS)
    else:
        mode = "echo" if ECHO else "capture-only"
    print("[game] listening on %s:%d (%s)" % (host, port, mode), flush=True)
    print("[game] captures -> %s" % CAPTURE_DIR, flush=True)
    return srv


def main():
    global ECHO, UPSTREAM
    # 控制台编码：抓包里可能出现任意字节，避免 print 因编码异常中断服务
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:]
    global RESPOND_CYCLE, MS_MODE
    if "--echo" in argv:
        ECHO = True
    if "--respond-cycle" in argv:
        RESPOND_CYCLE = True
    global MS_BRUTE, MS_BIND_PROBE
    if "--ms" in argv:
        MS_MODE = True
    if "--ms-brute" in argv:
        MS_MODE = True
        MS_BRUTE = True
    if "--ms-bind" in argv:
        MS_MODE = True
        MS_BRUTE = True
        MS_BIND_PROBE = True
    global GATE_OK, MS_OK
    if "--gate-ok" in argv:
        GATE_OK = True
    if "--ms-ok" in argv:
        MS_MODE = True
        MS_OK = True
    for i, a in enumerate(argv):
        if a == "--proxy" and i + 1 < len(argv):
            hp = argv[i + 1]
            h, _, p = hp.rpartition(":")
            UPSTREAM = (h, int(p))

    # ---- 脚手架开关（默认全关，走和真实游戏一致的开局）----
    # 见 game_server/config.py 的 Scaffold 注释与 game_server/scaffold.py。
    _unlock_all = "--unlock-all" in argv
    _level = None
    _tasks_done = None
    for i, a in enumerate(argv):
        if a == "--level" and i + 1 < len(argv):
            try:
                _level = int(argv[i + 1])
            except Exception:
                print("[cfg] --level 需要整数", flush=True)
        if a == "--tasks-done":
            _tasks_done = True
        if a == "--no-tasks-done":
            _tasks_done = False
    if "--no-scaffold" in argv:
        _unlock_all = False
        _level = None
        _tasks_done = False
    _sc = ooconf.configure_scaffold(unlock_all=_unlock_all, level=_level,
                                    all_tasks_done=_tasks_done)
    print("[cfg] 脚手架: %s" % _sc.describe(), flush=True)
    if MS_MODE:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        db.init()
        # ms 服务端口
        ms_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ms_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ms_srv.bind(("0.0.0.0", MS_PORT))
        ms_srv.listen(64)
        # 网关探测端口
        gate_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        gate_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        gate_srv.bind(("0.0.0.0", GATE_PORT))
        gate_srv.listen(64)
        print("[ms] ms service  listening on 0.0.0.0:%d" % MS_PORT, flush=True)
        print("[ms] gate probe  listening on 0.0.0.0:%d  (连接出现即成功)" % GATE_PORT, flush=True)
        print("[ms] captures -> %s" % CAPTURE_DIR, flush=True)

        def gate_loop():
            while True:
                c, a = gate_srv.accept()
                threading.Thread(target=_handle_gate_probe,
                                 args=(c, a, _next_id()), daemon=True).start()

        threading.Thread(target=gate_loop, daemon=True).start()
        try:
            while True:
                c, a = ms_srv.accept()
                threading.Thread(target=_handle_ms,
                                 args=(c, a, _next_id()), daemon=True).start()
        except KeyboardInterrupt:
            print("[ms] shutting down", flush=True)
        return

    srv = serve()
    try:
        while True:
            conn, addr = srv.accept()
            cid = _next_id()
            threading.Thread(target=_handle, args=(conn, addr, cid), daemon=True).start()
    except KeyboardInterrupt:
        print("[game] shutting down", flush=True)
    finally:
        srv.close()


if __name__ == "__main__":
    main()
