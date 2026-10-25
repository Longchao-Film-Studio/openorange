"""微服务（GameLoadService / UserDataService）连接处理（P0 遗留清理：从 server.py 拆出）。

协议已完全解出（见 game_server/ms/ 头注释）：
  帧: [u32 LE 载荷长度][u16 LE 类型=3][protobuf]
  请求: 1=seqId 2=服务名 3=方法名 4=参数(msgpack) 5=保留 8=选项
  应答: MsResponse {1: rid, 2: msgpack params, 4: service_id}

★ 早期协议未知时这里做过「帧类型爆破」（build_candidates / build_by_type 那一批），
  已全部删除；现在只有一条正确路径。
"""
import os
import struct
import time

from .. import config as ooconf
from .. import unknown_log as _ulog
from .. import ms as msmod
from . import accounts
from .net import CAPTURE_DIR, maybe_hexdump, recv_exact

from shared.log_tee import get_logger
_log = get_logger(__name__)


# ---------------------------------------------------------------- 未实现微服务方法（雷2）
# ★ 2026-10-23：以前未实现的方法一律回 {"Ret":[True,0]}。
#   MailService.setMailGiftReceived 是活生生的数据丢失案例：
#   客户端收到 status=true 就本地把 giftReceived 标成 true
#   （avatarmailcomp.lua:339-342），服务端没发奖 -> 奖励永久丢失。
#
#   所以这里也有硬黑名单：这些方法即使兼容模式也回失败。
#   严格模式（默认）/ 兼容模式（--unknown-ok）对"其余未知方法"生效。
_NEVER_FAKE_OK_MS = frozenset((
    "MailService.setMailGiftReceived",   # 客户端本地标已领 -> 奖励丢失
    "MailService.deleteMail",            # 客户端本地删邮件
    "MailService.setAllMailHaveRead",
    "MailService.setMailHaveRead",
    "MailService.star",
    "MailService.unstar",
))


def unknown_ms_params(service, method, uid=None, params=None):
    """未实现微服务方法的应答参数（msgpack bytes）：记日志 + 按模式决定成败。"""
    key = "%s.%s" % (service, method)
    fake = ooconf.unknown_fake_ok() and (key not in _NEVER_FAKE_OK_MS)
    _ulog.record("ms", key, uid=uid, params=params, fake_ok=fake,
                 reply="ack" if fake else "fail")
    if fake:
        return msmod.mp_map({"Ret": [True, 0]})
    return msmod.mp_map({"Ret": [False, "not implemented"]})


def handle_ms(conn, addr, cid):
    """微服务连接：解析请求帧并回 MsResponse。"""
    t0 = time.time()
    cap_path = os.path.join(CAPTURE_DIR, "ms_%03d_%s.bin" % (cid, addr[1]))
    _log.info("[ms] #%03d OPEN from %s:%d" % (cid, addr[0], addr[1]))
    f = None
    try:
        f = open(cap_path, "wb")
    except Exception:
        pass

    try:
        conn.settimeout(60)
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None:
                _log.info("[ms] #%03d peer closed" % cid)
                break
            ln = struct.unpack("<I", hdr)[0]
            if ln <= 0 or ln > 1 << 20:
                _log.info("[ms] #%03d bad length %d" % (cid, ln))
                break
            payload = recv_exact(conn, ln)
            if payload is None:
                _log.info("[ms] #%03d truncated payload" % cid)
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
                _log.warning("[ms] #%03d protobuf parse failed: %s" % (cid, e))
                _log.info(maybe_hexdump(payload, limit=256))
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
            _log.info(maybe_hexdump(payload, limit=256))

            # 按方法名区分负载：
            #   syncMetadata 的 Lua 回调 onSyncMetadataCb 只读 retStatus，
            #     给它塞 ServerList 属于多余数据，可能引起解析侧异常；
            #   query 的 onGetServerGates 才需要 ServerList + ClusterId。
            if method == "query":
                # 从 query 参数里抓账号名（field 4 是 msgpack，含 Username）
                if 4 in fields:
                    try:
                        _q = msmod.mp_decode(fields[4][0])[0]
                        if isinstance(_q, dict) and _q.get("Username"):
                            accounts.remember_username(_q["Username"])
                            print("[ms] #%03d 账号 %s" % (cid, _q["Username"]),
                                  flush=True)
                    except Exception:
                        pass
                params = msmod.build_load_service_params(ooconf.GATE_PORT)
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
            elif method == "fetchFriends":
                # ★ P1-7（2026-10-23）：FriendService 最小实现。
                #   客户端结算时必调 fetchFriends
                #   （avatarsettlementcomp.lua:36 -> avatarfriendcomp.lua:256-264
                #    callService("FriendService", "fetchFriends", {uid,start,limit})）。
                #   之前落进 else 回 {"Ret":[True,0]} 且无 Friends 字段，
                #   客户端 CheckFriendStatus 能过但 response.Friends 为空；
                #   这里回标准空好友列表，语义完整。
                #   响应结构见 core/microservice/msgateclientrpchandler.lua:35-49：
                #     response=decode(parameters); ret=response.Ret;
                #     retStatus={status=ret[1], errmsg=ret[2]}; response.Ret=nil
                params = msmod.mp_map({"Ret": [True, ""], "Friends": []})
            elif method == "queryNotification":
                # ★ P1-7 补充（2026-10-23）：FriendService.queryNotification。
                #   结算后客户端会调（avatarfriendcomp.lua:553）：
                #      callService("FriendService", "queryNotification", {uid},
                #                  cb, {timeout=5})
                #   回调 FriendService_queryNotification_cb 读 response.Notification。
                #   之前走 else 返回 {"Ret":[True,0]} 无字段 -> Notification 为 nil，
                #   这里给空通知列表，语义完整。
                params = msmod.mp_map({"Ret": [True, ""], "Notification": []})
            else:
                # ★ 雷2：未实现的微服务方法（记日志 + 模式/黑名单决定成败）
                params = unknown_ms_params(
                    service, method, uid=None,
                    params=fields.get(4, [b""])[0])
            pkt = msmod.frame_ms_response(seq, params, service)
            try:
                conn.sendall(pkt)
                print("[ms] #%03d TX MsResponse type=3 rid=%s service=%s method=%s (%d B)"
                      % (cid, seq, service, method, len(pkt)), flush=True)
            except Exception as e:
                _log.warning("[ms] #%03d send failed: %s" % (cid, e))

    except Exception as e:
        _log.warning("[ms] #%03d error: %s" % (cid, e))
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
        print("[ms] #%03d CLOSE after %dms" % (cid, int((time.time() - t0) * 1000)),
              flush=True)
