"""游戏网关连接处理（P0 遗留清理：从 server.py 拆出）。

一条 gate 连接的生命周期：
  reqType=0 NEW_CONNECTION -> 回 ConnectServerResp + bindAccount
  reqType=4 BIND_ACCOUNT   -> 回 ConnectServerResp + bindAvatar
  reqType=3 BIND_AVATAR    -> 回 ConnectServerResp + createChannelEntity
  reqType=2 BIND_SOUL      -> 绑定完成
之后这条连接上继续跑实体 RPC（帧类型 4），以及每秒一次的心跳（帧类型 6）。

应答格式全部来自二进制里的 client_gate.proto 描述符，不靠猜：
  message ConnectServerResp { RespType type = 1; string sessionId = 2; }
  enum RespType { Busy=0; Connected=1; ConnectRefused=2; Reconnected=3; ... }
"""
import struct
import time

from .. import config as ooconf
from .. import entity_rpc as erpc
from .. import ms as msmod
from ..rpc_common import SYNC_SELF
from . import accounts
from .net import maybe_hexdump

from shared.log_tee import get_logger
_log = get_logger(__name__)

try:
    from shared import db as shared_db
except Exception:
    shared_db = None


def handle_gate(conn, addr, cid):
    """游戏网关：绑定链 + 实体 RPC。"""
    t0 = time.time()
    _log.info("")
    _log.info("=" * 64)
    _log.info("[GATE] ★ 收到网关连接 %s:%d" % (addr[0], addr[1]))
    _log.info("=" * 64)
    try:
        conn.settimeout(60)
        buf = bytearray()
        pending = []          # 已切出来、还没处理的完整帧

        def next_frame():
            """按 [u32 LE payloadLen][u16 LE msgType][payload] 切出**一帧**。

            以前是把一次 recv 的数据整块当一帧处理。实测客户端会把两条实体
            消息粘在同一个 TCP 段里发（77 B = Cmd_Avatar_StartGame_CS + 心跳），
            于是第二条被 protobuf 解析失败直接丢掉 —— 丢的正好是
            networkCheckAfterChange_CS(index=0)，客户端 _recvHeartbeatIndex
            就停在 0，差值越拉越大，最后触发弱网重连。

            ★ 2026-10-22 修：原来写成「切出 frames 就 return frames[0]」，
              一次 recv 里有**两条完整帧**时第二条被直接丢掉（rest 只留半包）。
              现在先全部塞进 pending 队列，再一条条返回。
            """
            nonlocal buf
            while not pending:
                frames, rest = msmod.split_frames(buf)
                if frames:
                    buf[:] = rest
                    pending.extend(frames)
                    break
                chunk = conn.recv(8192)
                if not chunk:
                    return b""
                buf += chunk
            return pending.pop(0)

        while True:
            data = next_frame()
            if not data:
                _log.info("[GATE] #%03d peer closed" % cid)
                break
            _log.debug("[GATE] #%03d RX %d B:" % (cid, len(data)))
            _log.info(maybe_hexdump(data, limit=512))

            frame_type = None
            proto_req = None
            fields = {}
            if len(data) >= 6:
                frame_type = struct.unpack_from("<H", data, 4)[0]
                # ★ 真正的 reqType 在 protobuf 字段 1 里（08 XX），不是帧头
                try:
                    fields = msmod.pb_parse(data[6:])
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
                    print("[GATE] #%03d 帧类型=%d  protobuf 解析失败: %s"
                          % (cid, frame_type, e), flush=True)

            _bind_account(cid, proto_req, fields)
            _dispatch_frame(conn, cid, data, frame_type, proto_req)
            _push_bind_message(conn, addr, cid, proto_req)
    except Exception as e:
        _log.warning("[GATE] #%03d error: %s" % (cid, e))
    finally:
        dur = int((time.time() - t0) * 1000)
        # 连接断开就从在线表里摘掉，并释放账号关联
        accounts.release(cid)
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


def _bind_account(cid, proto_req, fields):
    """把这条连接和账号绑起来。

    ★ 多账号（P0 修复，2026-10-23）：**优先认 auth，猜队列只当兜底**。
      注入 Lua 已给 NEW_CONNECTION（reqType=0）的 connectServer 补上
      Mailbox{1:"account_<account_id>"}，服务端在这里直接还原账号——
      两个客户端同时登录不再靠 30 秒窗口猜，也不会串号。
      旧链路（reqType 2/3/4）本来就是从 auth 还原，逻辑不变。
    """
    if proto_req == 0:
        # ★ 新路径：从 auth 认号（account_id -> 登录名）。认不到才走队列。
        uid = accounts.uid_from_auth_display(fields)
        if uid and not accounts.is_bound(cid):
            accounts.bind(cid, uid)
            print("[GATE] #%03d 从 auth 绑定账号 %s（reqType=0）"
                  % (cid, uid), flush=True)
            return
        # 兜底：老客户端 / auth 没带上时，退回「ms query 时序 + 30 秒窗口」猜队列
        claimed = accounts.claim_and_bind(cid)
        print("[GATE] #%03d 关联账号 %s（队列兜底）"
              % (cid, claimed or "(队列为空，回退 %s)" % accounts.DEFAULT_UID),
              flush=True)
    elif proto_req in (2, 3, 4) and not accounts.is_bound(cid):
        uid = accounts.uid_from_auth_display(fields)
        if uid:
            accounts.bind(cid, uid)
            _log.info("[GATE] #%03d 从 auth 还原账号 %s" % (cid, uid))


def _dispatch_frame(conn, cid, data, frame_type, proto_req):
    """按帧类型分派：心跳 / 连接帧 / 实体 RPC。"""
    is_heartbeat_frame = (frame_type == 6)
    is_connect_frame = (frame_type == 3 and proto_req is not None)
    if is_heartbeat_frame:
        _on_heartbeat(conn, cid)
    elif is_connect_frame:
        _on_connect(conn, cid)
    else:
        _on_entity_rpc(conn, cid, data, frame_type)


def _on_heartbeat(conn, cid):
    """type=6：sendHeartbeat 是单向的，不回；顺带刷在线表和体力恢复。"""
    if shared_db is not None:
        try:
            shared_db.touch_game_online("gate-%03d" % cid)
        except Exception:
            pass

    # ★ 体力随时间恢复：借用心跳（每秒一次）做驱动。
    #   放在这里而不是后台线程，是因为**必须在本连接的线程里写 socket**，
    #   否则和 RPC 应答并发写会把帧切坏。
    #   apply_recovery 只有真的到点（每 KeyRefresh=360 秒）才会返回 >0，
    #   所以不会每秒钟都推包。
    uid = accounts.uid_of(cid)
    if uid != ooconf.FALLBACK_UID:
        try:
            from .. import player_state as _pstate
            from .. import stamina as stamina_mod
            # ★ 雷5：体力恢复是「读-改-写」，和 RPC 并发时会丢更新。
            #   锁内只改状态 + 造帧，sendall 放到锁外（不锁 I/O）。
            frames = []
            gained = 0
            with _pstate.lock_for(uid):
                st = erpc._state(uid)
                lv = int(st.props.get("level") or 1)
                gained, _ = stamina_mod.apply_recovery(st.props, lv)
                if gained:
                    st.save()
                    for prop in (stamina_mod.PROP_KEYS, stamina_mod.PROP_NEXT,
                                 stamina_mod.PROP_TOTAL):
                        # ★ 走统一入口 _sync_frame（剥私有键 + name 转 str）
                        frames.append(erpc._sync_frame(
                            SYNC_SELF, 0, msmod.OP_CHANGE, prop,
                            st.props.get(prop)))
                    keys_now = st.props.get(stamina_mod.PROP_KEYS)
            for pkt in frames:
                conn.sendall(pkt)
            if gained:
                print("[GATE] #%03d 体力恢复 +%d -> %s"
                      % (cid, gained, keys_now), flush=True)
        except Exception as _e:
            _log.warning("[GATE] #%03d 体力恢复失败: %s" % (cid, _e))
    _log.info("[GATE] #%03d 心跳 type=6，不回" % cid)


def _on_connect(conn, cid):
    """type=3：回 ConnectServerResp {1: Connected, 2: sessionId, 5: MSGPACK}。

    content_type 必须显式写出（proto2 has-field 语义），否则客户端判为无效。
    """
    sid = accounts.uid_of(cid)
    pkt = msmod.frame_connect_resp(1, sid)
    conn.sendall(pkt)
    print("[GATE] #%03d TX ConnectServerResp {1:Connected, 2:%s, 5:MSGPACK} (%d B)"
          % (cid, sid, len(pkt)), flush=True)
    _log.info(maybe_hexdump(pkt, limit=256))


def _on_entity_rpc(conn, cid, data, frame_type):
    """帧类型 4：实体 RPC。时间同步和心跳应答在这里特判，其余交给实体层。"""
    idx, params, rid = msmod.parse_entity_frame(data[6:])
    print("[GATE] #%03d 实体消息 type=%d index=%s rid=%s params=%s"
          % (cid, frame_type, idx, rid,
             params.hex() if isinstance(params, bytes) else params), flush=True)
    if idx is None:
        return

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
            _log.warning("[GATE] #%03d 解析客户端时间失败: %s" % (cid, e))
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
            _log.warning("[GATE] #%03d 解析心跳序号失败: %s" % (cid, e))
        rpkt = msmod.build_net_check_ack(hb)
        label = "networkCheckAfterChange_SC(%d)" % hb
    elif idx == msmod.RPC_INDEX_NOT_FOUND:
        # __rpc_index_not_found__：客户端在说「你发了个我不认识的 rpc」。
        # ★ 必须显式 no-op：这类帧 rid 可能 > 0，交给 erpc.handle 会落进
        #   dispatch 的「未知索引回通用 ACK」分支，客户端会收到一条不该有的
        #   __rpc_response__。父提交里这段被粘进注释吞掉了，本次恢复。
        rpkt = None
        label = None
    else:
        # 其余全部交给实体 RPC 层：带 rid 的一律回 entityMessageResponse，
        # 单向的返回空列表。
        rpkt = erpc.handle(idx, params, rid, uid=accounts.uid_of(cid))
        label = "%s(rid=%d)" % (erpc.describe(idx), rid)

    # ★ rpkt 有两种形态：
    #   - 裸 bytes：时间同步 / 心跳应答；
    #   - 帧列表：entity_rpc.handle 返回（应答帧 + 可能的属性同步帧）。
    # 以前直接 for 遍历，裸 bytes 会被逐字节当成 int 发出去，
    # 报 "a bytes-like object is required, not 'int'" 并断连。
    if isinstance(rpkt, (bytes, bytearray)):
        pkts = [rpkt]
    elif rpkt:
        pkts = list(rpkt)
    else:
        pkts = []
    for pkt in pkts:
        conn.sendall(pkt)
        _log.debug("[GATE] #%03d TX %s (%d B)" % (cid, label, len(pkt)))
        _log.info(maybe_hexdump(pkt, limit=256))
    if len(pkts) > 1:
        print("[GATE] #%03d TX %s + %d 条属性同步"
              % (cid, label, len(pkts) - 1), flush=True)
    elif not pkts:
        _log.info("[GATE] #%03d %s 无需应答" % (cid, erpc.describe(idx)))


def _push_bind_message(conn, addr, cid, proto_req):
    """按 reqType 推进绑定链：0->bindAccount, 4->bindAvatar, 3->createChannelEntity。

    ★ 实测（work/oo_analysis/gamesrv_run.log）：
      reqType=0 + 下发 bindAccount -> 客户端接受，随即用 reqType=4 重连（正常）
      reqType=4 + 下发 bindAvatar  -> 客户端 16ms 后主动断开重连（正常）
      reqType=3 + 什么都不发       -> 客户端 3700ms 后超时断开重连（卡点）
    3.7 秒正好是客户端的连接超时，说明它在等一条消息 —— 就是 createChannelEntity
    （gateclientrpchandler.lua:createChannelEntity / IGateClientService 第 4 个方法）。
    """
    sid = accounts.uid_of(cid)
    label = None
    bpkt = None
    if proto_req == 0:
        # mailbox 名字带上账号，客户端会把 auth 原样回传，
        # 便于在日志里确认「这条连接确实是这个账号」
        bpkt = msmod.frame_bind_account(
            session_id=sid,
            act_mailbox=msmod.build_entity_mailbox(
                entity_id="account_%s" % sid))
        label = "bindAccount"
    elif proto_req == 4:
        bpkt = msmod.frame_bind_avatar(
            session_id=sid,
            avt_mailbox=msmod.build_entity_mailbox(
                entity_id="avatar_%s" % sid))
        label = "bindAvatar"
    elif proto_req == 3:
        bpkt = msmod.build_channel_entity(
            entity_id="avatar_%s" % sid,
            session_id=sid,
            uid=sid)
        label = "createChannelEntity"
    if bpkt is None:
        return
    conn.sendall(bpkt)
    print("[GATE] #%03d TX %s  (reqType=%d) (%d B)"
          % (cid, label, proto_req, len(bpkt)), flush=True)
    _log.info(maybe_hexdump(bpkt, limit=256))
    # ★ 上报游戏内在线：createChannelEntity 一发出去，
    #   客户端就已经在实体层了（onBecomePlayer 随后触发）。
    if label == "createChannelEntity" and shared_db is not None:
        try:
            shared_db.set_game_online(
                session_id="gate-%03d" % cid,
                username=sid,
                uid=sid,
                entity_id=ooconf.avatar_entity_id(sid),
                addr="%s:%d" % (addr[0], addr[1]))
            _log.info("[GATE] #%03d 在线登记 %s" % (cid, sid))
        except Exception as _e:
            _log.warning("[GATE] #%03d 在线登记失败: %s" % (cid, _e))
