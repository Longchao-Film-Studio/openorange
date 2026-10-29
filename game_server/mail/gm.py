#!/usr/bin/env python3
"""GM 发信（M4）：在还没有任何"发信入口"的阶段，给测试与运营提供造数据的唯一手段。

客户端入口（avatardebugcomp.lua:98-113）：

    GMSendMailToPlayer(mailId) -> serverRpc("GmRpcSendMailToPlayer", {mailId})
    GMSendMailToGroup(mailId)  -> serverRpc("GmRpcSendMailToGroup",  {mailId})

★ 这两条以前没登记在 rpc_index 里，落进 unknown_rpc_ret：
  严格模式回 not implemented（游戏里 GM 命令毫无反应），
  兼容模式**假成功**（客户端以为发了，其实没有）—— 后者更糟。

★ 发信本身不改任何玩家根属性，所以不回属性同步帧；
  邮件是客户端主动拉取的（getUserMailList），下一次打开面板就能看到。
"""
from __future__ import annotations

from shared.log_tee import get_logger

from ..rpc_common import decode_params, respond_after_sync, state
from ..transport import online as _online
from . import send as mailsend

_log = get_logger(__name__)


def _template_id(params) -> int:
    """参数形如 {mailId}。解不出就当 0（后面会被 send_mail 判成模板不存在）。"""
    vals = decode_params(params)
    if not isinstance(vals, list) or not vals:
        return 0
    try:
        return int(vals[0])
    except (TypeError, ValueError):
        return 0


def _gm_send_mail_to_player(rid, params, uid=None):
    """GmRpcSendMailToPlayer {mailId}：给自己发一封模板邮件。"""
    tid = _template_id(params)
    st = state(uid)
    sent = mailsend.send_mail(st, tid)
    if not sent:
        _log.warning("[mail] GM 发信失败：模板 %s 不存在（uid=%s）", tid, uid)
        return respond_after_sync(rid, [], ret={"status": False,
                                                "errmsg": f"mail template {tid} not found"})
    st.save()
    _log.info("[mail] GM 发信 uid=%s 模板=%s MailId=%s", uid, tid,
              [m["MailId"] for m in sent])
    return respond_after_sync(rid, [], ret={"status": True})


def _gm_send_mail_to_group(rid, params, uid=None):
    """GmRpcSendMailToGroup {mailId}：给当前所有在线玩家各发一封。

    ★ "群发"在私服里的语义就是**在线的人**：离线玩家的邮件没有全局广播通道
      （群邮件需要 M5 的定时/条件求值），这里不做假装。
    """
    tid = _template_id(params)
    targets = list(_online.uids())
    if uid and uid not in targets:
        targets.append(uid)                      # 调用者自己也算一个
    ok = 0
    for target in targets:
        st = state(target)
        if mailsend.send_mail(st, tid):
            st.save()
            ok += 1
    _log.info("[mail] GM 群发 模板=%s 目标 %d 人，成功 %d（发起人 uid=%s）",
              tid, len(targets), ok, uid)
    return respond_after_sync(rid, [], ret={"status": ok > 0,
                                            "errmsg": "" if ok else f"mail template {tid} not found"})
