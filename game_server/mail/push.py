#!/usr/bin/env python3
"""邮件推送（M6）：新邮件到达时主动告诉在线玩家。

## 链路

    服务端                                  客户端
    send_mail() 落库后
      -> mail.push.notify_new_mail(st, mails)
      -> ms/frames.frame_ms_push(type=4, MsPush)
      -> transport.ms_conns.push(uid, frame)      ---- TCP ---->  MsGateClientRpcHandler
                                                                    -> MsServiceManager:onPush
                                                                       method = "MailService_newMailNotice"
                                                                       target = ClientAvatar（注入脚本把订阅者换成它）
                                                                       -> AvatarMailComp:MailService_newMailNotice
                                                                          addMail + 红点

## 三条前提（缺一不可，都是这一轮补的）

  1. 客户端要先 bind（ClientGate.bind 成功），否则不注册推送端点；
  2. msAuth 必须非空（prop_origin 派生），否则客户端根本不 bind；
  3. 订阅者必须换成 ClientAvatar（注入 Lua 挂钩 addPushEndpoint），
     否则 onPush 在 MsCommon 组件上找不到 MailService_newMailNotice，直接丢。

## 不在线怎么办

返回 False，**不当错误**：邮件已经落库，玩家下次登录拉列表就能看到。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from shared.log_tee import get_logger

from .. import config as _ooconf
from .. import ms as msmod
from ..ms import frames as _frames
from ..transport import ms_conns

if TYPE_CHECKING:
    from ..player_state import PlayerState

_log = get_logger(__name__)


#: 每个账号最近一次推送的结果（给管理页/命令回显用，不参与业务判断）
_LAST: dict[str, bool] = {}


def last_push_ok(st: PlayerState) -> bool:
    """这个账号最近一次推送成功了吗（只用于回显，**不要**拿它做业务分支）。"""
    return _LAST.get(str(st.props.get("uid") or ""), False)


def notify_new_mail(st: PlayerState, mails: list[dict]) -> bool:
    """把刚入库的邮件推给玩家（不在线返回 False）。

    参数形状要和客户端对齐（core/microservice/msservicemanager.lua:138-180）：
      onPush(orgType, epIds, msgId, serviceName, methodName, parameters)
      func = target["MailService_newMailNotice"]
      SafeCallback(func, target, unpack(parameters or {}))
    所以 parameters 必须是数组，且第一个元素就是 newMailNotice 要的 mailsInfo。
    """
    if not mails or not _ooconf.mail_push_enabled():
        return False
    uid = str(st.props.get("uid") or "")
    if not uid:
        _log.warning("[mail] 推送跳过：状态里没有 uid")
        return False
    params = msmod.mp_arr([list(mails)])
    frame = _frames.frame_ms_push("ENTITY", [uid], "MailService", "newMailNotice", params)
    ok = ms_conns.push(uid, frame)
    _LAST[uid] = ok
    if not ok:
        _log.info("[mail] uid=%s 不在 MS 在线表里，跳过推送（邮件已落库）", uid)
    return ok


def push_to_group(uids, mails: list[dict]) -> int:
    """给一组账号推同一批邮件，返回成功推送的条数（群发用）。"""
    if not mails or not uids or not _ooconf.mail_push_enabled():
        return 0
    params = msmod.mp_arr([list(mails)])
    ok = 0
    for uid in uids:
        frame = _frames.frame_ms_push("ENTITY", [str(uid)], "MailService", "newMailNotice", params)
        if ms_conns.push(str(uid), frame):
            ok += 1
    return ok
