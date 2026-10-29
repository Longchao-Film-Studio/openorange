"""微服务（GameLoadService / UserDataService）连接处理（P0 遗留清理：从 server.py 拆出）。

协议已完全解出（见 game_server/ms/ 头注释）：
  帧: [u32 LE 载荷长度][u16 LE 类型=3][protobuf]
  请求: 1=seqId 2=服务名 3=方法名 4=参数(msgpack) 5=保留 8=选项
  应答: MsResponse {1: rid, 2: msgpack params, 4: service_id}

★ 早期协议未知时这里做过「帧类型爆破」（build_candidates / build_by_type 那一批），
  已全部删除；现在只有一条正确路径。
"""
import contextlib
import os
import struct
import time

from shared.log_tee import get_logger

from .. import config as ooconf
from .. import ms as msmod
from .. import unknown_log as _ulog
from . import accounts
from .net import CAPTURE_DIR, maybe_hexdump, recv_exact

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

# ★ 2026-10-29：整服务黑名单。逐个列方法会漏 —— 客户端一共有 11 个邮件方法，
#   上面只列了 6 个；漏掉的 getUserMailList / getMailContent / syncGroupMail /
#   queryNotification / readNotification 在兼容模式下会回 {"Ret":[true,0]}，
#   而客户端收到 status=true 就继续往下走：
#     getUserMailList 假成功 -> handleOnePageMailList 里 pairs(response.MailList) 是 nil -> Lua 报错
#     readNotification 假成功 -> 红点假熄灭
#   邮件这一整个服务都属于「假成功 = 静默丢东西 / 状态错」，所以按服务拒绝，不按方法列。
_NEVER_FAKE_OK_SERVICES = frozenset((
    "MailService",
))


def unknown_ms_params(service, method, uid=None, params=None):
    """未实现微服务方法的应答参数（msgpack bytes）：记日志 + 按模式决定成败。"""
    key = f"{service}.{method}"
    fake = (ooconf.unknown_fake_ok()
            and key not in _NEVER_FAKE_OK_MS
            and service not in _NEVER_FAKE_OK_SERVICES)
    _ulog.record("ms", key, uid=uid, params=params, fake_ok=fake,
                 reply="ack" if fake else "fail")
    if fake:
        return msmod.mp_map({"Ret": [True, 0]})
    return msmod.mp_map({"Ret": [False, "not implemented"]})


def _log_dump(payload, limit=256):
    """hexdump 只在 DEBUG 且 PACKET_LOG 开启时产出 —— 单独成函数，别抬高调用方复杂度。"""
    _dump = maybe_hexdump(payload, limit=limit)
    if _dump:
        _log.debug("%s", _dump)


# ---------------------------------------------------------------- 分派表（唯一入口）
# ★ 2026-10-29：以前这里是一串 `if method == "query" / elif method == "batchGetAttribute" ...`，
#   **只看 method，不看 service**。后果：`MailService.queryNotification`
#   （avatarmailcomp.lua:527）命中了 FriendService 的分支，收到好友语义的空通知列表 ——
#   而邮件那边把「Notification 有没有值」当成「有没有新邮件」，Lua 空表为真 ->
#   红点逻辑直接错。同类问题只要"两个服务有同名方法"就会再犯一次。
#
#   现在分派键是 (service, method)。未登记的组合一律走 unknown_ms_params，
#   新增一个服务必须显式登记，别名/同名不会再被别的服务吞掉。


def _reply_game_load_query(fields, cid):
    """GameLoadService.query：下发服务器列表，顺带记下账号（多账号关联要用）。"""
    if 4 in fields:
        try:
            _q = msmod.mp_decode(fields[4][0])[0]
            if isinstance(_q, dict) and _q.get("Username"):
                accounts.remember_username(_q["Username"])
                _log.info('[ms] #{:03d} 账号 {}'.format(cid, _q["Username"]))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            pass
    return msmod.build_load_service_params(ooconf.GATE_PORT)


def _reply_batch_get_attribute(fields, cid):
    """UserDataService.batchGetAttribute：玩家数据主通道（avataruserdatacomp.lua:151）。"""
    _req = []
    if 4 in fields:
        try:
            _req = msmod.mp_decode(fields[4][0])[0]
        except Exception as _e:
            _log.warning(f'[ms] #{cid:03d} batchGetAttribute 参数解析失败: {_e}')
            _req = []
    params = msmod.build_userdata_batch_params(_req)
    _log.info('[ms] #{:03d}    -> 回 {:d} 条 Results'.format(
        cid, len(msmod.mp_decode(params)[0].get("Results", []))))
    return params


def _reply_fetch_friends(_fields, _cid):
    """FriendService.fetchFriends：结算时必调，回标准空好友列表。

    客户端 avatarsettlementcomp.lua:36 -> avatarfriendcomp.lua:256-264。
    以前落进 else 回 {"Ret":[True,0]} 且无 Friends 字段：CheckFriendStatus 能过，
    但 response.Friends 是 nil。
    """
    return msmod.mp_map({"Ret": [True, ""], "Friends": []})


def _reply_friend_query_notification(_fields, _cid):
    """FriendService.queryNotification：回空通知列表。

    ★ 只服务好友。邮件的**同名方法**在 MailService 下，不在这张表里，
      会走 unknown_ms_params —— 这正是修掉「一个分支同时服务两个服务」的地方。
    """
    return msmod.mp_map({"Ret": [True, ""], "Notification": []})


#: (service, method) -> 应答参数构造函数。**唯一分派点**。
_MS_HANDLERS = {
    ("GameLoadService", "query"): _reply_game_load_query,
    ("UserDataService", "batchGetAttribute"): _reply_batch_get_attribute,
    ("FriendService", "fetchFriends"): _reply_fetch_friends,
    ("FriendService", "queryNotification"): _reply_friend_query_notification,
}


def ms_reply_params(service, method, fields, cid=0):
    """(service, method) -> MsResponse 的 msgpack 参数。

    未登记的组合一律按「未实现」处理（记日志 + 按模式/黑名单决定成败）。
    单独成函数是为了能被单测直接调用 —— 不需要起 socket 就能验分派。
    """
    handler = _MS_HANDLERS.get((service, method))
    if handler is not None:
        return handler(fields, cid)
    return unknown_ms_params(service, method, uid=None,
                             params=fields.get(4, [b""])[0])


def handle_ms(conn, addr, cid):
    """微服务连接：解析请求帧并回 MsResponse。"""
    t0 = time.time()
    cap_path = os.path.join(CAPTURE_DIR, f'ms_{cid:03d}_{addr[1]}.bin')
    _log.info(f'[ms] #{cid:03d} OPEN from {addr[0]}:{addr[1]:d}')
    f = None
    with contextlib.suppress(Exception):
        f = open(cap_path, "wb")

    try:
        conn.settimeout(60)
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None:
                _log.info(f'[ms] #{cid:03d} peer closed')
                break
            ln = struct.unpack("<I", hdr)[0]
            if ln <= 0 or ln > 1 << 20:
                _log.info(f'[ms] #{cid:03d} bad length {ln:d}')
                break
            payload = recv_exact(conn, ln)
            if payload is None:
                _log.info(f'[ms] #{cid:03d} truncated payload')
                break

            mtype = struct.unpack("<H", payload[:2])[0]
            body = payload[2:]
            if f is not None:
                with contextlib.suppress(Exception):
                    f.write(hdr + payload)
                    f.flush()

            try:
                fields = msmod.pb_parse(body)
            except Exception as e:
                _log.warning(f'[ms] #{cid:03d} protobuf parse failed: {e}')
                _log_dump(payload)
                continue

            seq = fields.get(1, [0])[0]
            service = fields.get(2, [b""])[0]
            method = fields.get(3, [b""])[0]
            with contextlib.suppress(Exception):
                service = service.decode("utf-8", "replace")
                method = method.decode("utf-8", "replace")

            _log.debug("[ms] #%03d RX type=%d seq=%s service=%s method=%s  (%d B)",
                       cid, mtype, seq, service, method, len(payload))
            _log_dump(payload)

            # 分派：键是 (service, method)，未登记的组合按「未实现」处理。
            #   syncMetadata 的 Lua 回调只读 retStatus，不需要额外字段；
            #   query 的 onGetServerGates 才需要 ServerList + ClusterId ——
            #   各自在 _MS_HANDLERS 里负责，不再在这里按 method 链式判断。
            params = ms_reply_params(service, method, fields, cid)
            pkt = msmod.frame_ms_response(seq, params, service)
            try:
                conn.sendall(pkt)
                _log.info(f'[ms] #{cid:03d} TX MsResponse type=3 rid={seq} service={service} method={method} ({len(pkt):d} B)')
            except Exception as e:
                _log.warning(f'[ms] #{cid:03d} send failed: {e}')

    except Exception as e:
        _log.warning(f'[ms] #{cid:03d} error: {e}')
    finally:
        with contextlib.suppress(Exception):
            conn.close()
        if f is not None:
            with contextlib.suppress(Exception):
                f.close()
        _log.info(f'[ms] #{cid:03d} CLOSE after {int((time.time() - t0) * 1000):d}ms')
