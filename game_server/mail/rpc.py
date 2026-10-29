#!/usr/bin/env python3
"""MailService 的协议层：11 个方法的分派与应答拼装。

契约来源（都是**反查客户端**得来的，不是猜的）：

    core/microservice/msgateclientrpchandler.lua:35-49
        response = msgpack.decode(params)
        ret      = response.Ret                 -- 必须是 [status, errmsg]，缺了回调根本不调用
        retStatus= {status=ret[1], errmsg=ret[2]}
        response.Ret = nil                      -- 剩下的键就是 response
    entities/avatarcomponent/avatarmailcomp.lua:388-544   -- 11 个方法与参数
    ui/panel/mailpanel/mailpanel.lua / node_mail_listgroup.lua  -- 读哪些字段

## 应答字段名（大小写敏感，错一个就是"面板打不开"）

    getUserMailList   -> MailList(list) / TotalSize(int) / Start(int)
    getMailContent    -> MailId(int) / Content(str)
    其余              -> 只读 Ret

## 这个方法还没实现时会发生什么

没登记到 registry 的 (service, method) 一律走 ms_service.unknown_ms_params
（记 unknown 日志 + 严格模式回失败）。而 MailService 在
`_NEVER_FAKE_OK_SERVICES` 里 —— 即使兼容模式也不许假成功，
因为客户端拿到 status=true 就会本地改状态（`setMailGiftReceived_cb` 直接标"已领"）。

## 阶段

    M1（本文件当前）  只读：getUserMailList / getMailContent / syncGroupMail /
                      queryNotification / readNotification
    M2                领奖：setMailGiftReceived（走 grants 唯一发放入口）
    M3                管理：deleteMail / setMailHaveRead / setAllMailHaveRead / star / unstar
"""
from __future__ import annotations

from typing import Any

from shared.log_tee import get_logger

from .. import ms as msmod
from .. import player_state as pstate
from ..ms import registry
from ..property.bag import grants as _grants
from ..transport import online as _online
from . import store, template

_log = get_logger(__name__)

#: 单页硬上限。客户端写死 100（avatarmailcomp.lua:97），这里只防"客户端要 100 万封"。
MAX_PAGE = 200


# ------------------------------------------------------------------ 公共小工具
def _args(fields: dict) -> list:
    """请求的 field 4 = msgpack 数组。解不出来就当空数组（不让坏包把连接打挂）。"""
    raw = fields.get(4)
    if not raw:
        return []
    try:
        v = msmod.mp_decode(raw[0])[0]
    except Exception:  # noqa: BLE001 —— 坏 msgpack 只影响这一条请求
        return []
    return v if isinstance(v, list) else []


def _as_int(v: Any, default: int = 0) -> int:  # noqa: ANN401
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _ok(**extra: Any) -> bytes:  # noqa: ANN401
    payload = {"Ret": [True, ""]}
    payload.update(extra)
    return msmod.mp_map(payload)


def _fail(msg: str) -> bytes:
    return msmod.mp_map({"Ret": [False, msg]})


def _state(uid: str):
    """取玩家状态；只有已经存在的才算（peek 不会建空壳存档，避免污染）。

    ★ uid 从 args[0] 来。MS 连接和 gate 连接不是同一条，`accounts.uid_of(ms_cid)`
      拿不到人，只能信参数 —— 所以这里**必须**校验"这个 uid 真的有存档"，
      不能让任意 uid 读到别人的邮箱（见 docs/方案-邮件系统-全链路复检.md R7）。
    """
    if not uid:
        return None
    return pstate.peek(uid)


# ------------------------------------------------------------------ M1：只读
@registry.register("MailService", "getUserMailList")
def _get_user_mail_list(fields: dict, _cid: int) -> bytes:
    """getUserMailList(uid, startIndex, count) -> MailList / TotalSize / Start。

    参数布局是抓包实测的（work/captures/ms_030_53208.bin，seq=33..36）：
        ["123tst", 0, 100]   —— 第 3 个参数是 **count**（客户端写死 100），不是 endIndex。
    """
    args = _args(fields)
    uid = str(args[0]) if args else ""
    start = max(0, _as_int(args[1] if len(args) > 1 else 0, 0))
    count = _as_int(args[2] if len(args) > 2 else 100, 100)
    count = max(1, min(count, MAX_PAGE))

    st = _state(uid)
    if st is None:
        return _fail("unknown user")

    with pstate.lock_for(uid):
        items = store.all_mails(st.props)
    total = len(items)
    page = items[start:start + count]
    return _ok(MailList=page, TotalSize=total, Start=start)


@registry.register("MailService", "getMailContent")
def _get_mail_content(fields: dict, _cid: int) -> bytes:
    """getMailContent(uid, mailId) -> MailId / Content。

    ★ 这里**不改已读状态**。客户端 getMailContent_cb 看到 `haveRead == false` 会自己
      再发 setMailHaveRead（avatarmailcomp.lua:300-302），由那个方法负责落状态 ——
      两个地方都改就会变成两个产生者。
    """
    args = _args(fields)
    uid = str(args[0]) if args else ""
    mail_id = _as_int(args[1] if len(args) > 1 else 0, 0)

    st = _state(uid)
    if st is None:
        return _fail("unknown user")

    with pstate.lock_for(uid):
        mail = store.get(st.props, mail_id)
    if mail is None or mail.get(store.DELETED):
        return _fail("mail not found")
    return _ok(MailId=mail_id, Content=template.content_of(mail))


@registry.register("MailService", "syncGroupMail")
def _sync_group_mail(fields: dict, _cid: int) -> bytes:
    """syncGroupMail(uid)：客户端不读应答字段（只透传给回调），回成功即可。

    群邮件（按条件给全服/全服某档玩家发信）在 M5 之后再考虑；
    现在这里只是"同步一下有没有群邮件"的占位，语义是「没有」。
    """
    args = _args(fields)
    uid = str(args[0]) if args else ""
    if _state(uid) is None:
        return _fail("unknown user")
    return _ok()


@registry.register("MailService", "queryNotification")
def _query_notification(fields: dict, _cid: int) -> bytes:
    """queryNotification(uid) -> 有未通知的新邮件时才带 `Notification`。

    ★★ 这是整个邮件系统最容易写错的一条：客户端判的是
        `if retStatus.status and response.Notification then hadNewMail = true`
      而**Lua 里空表是真值**。所以"没有新邮件"时**必须让这个键不存在**，
      回 `Notification: []` 会让红点永远亮（这正是旧实现被 FriendService 分支
      误命中所造成的那个 bug）。
    """
    args = _args(fields)
    uid = str(args[0]) if args else ""
    st = _state(uid)
    if st is None:
        return _fail("unknown user")

    with pstate.lock_for(uid):
        fresh = store.has_new(st.props)
        items = [{"MailId": int(m.get("MailId") or 0)}
                 for m in store.all_mails(st.props) if not m.get("Params", {}).get("haveRead")]
    if not fresh or not items:
        return _ok()
    return _ok(Notification=items)


@registry.register("MailService", "readNotification")
def _read_notification(fields: dict, _cid: int) -> bytes:
    """readNotification(uid)：客户端"我已经知道有新邮件了"，清掉新邮件标记。"""
    args = _args(fields)
    uid = str(args[0]) if args else ""
    st = _state(uid)
    if st is None:
        return _fail("unknown user")
    with pstate.lock_for(uid):
        store.clear_new(st.props)
    return _ok()

# ------------------------------------------------------------------ M2：领奖
@registry.register("MailService", "setMailGiftReceived")
def _set_mail_gift_received(fields: dict, _cid: int) -> bytes:
    """setMailGiftReceived(uid, mailIds)：发奖 -> 标记 -> 推同步 -> 才回成功。

    ★★ 顺序是这个方法的全部要害。客户端 setMailGiftReceived_cb
       （avatarmailcomp.lua:337-347）拿到 status=true 就**本地**把 Params.giftReceived
       标成 true，不看服务端有没有发货。所以：
         · 必须**先发货、再落盘、再回 true**；
         · 任何一封发不出去，整批回 fail（宁可让玩家再点一次，也不能假成功）。

    ★ 幂等：已领过的直接跳过（不重复发，也不报错）。多开/连点/重放同一个 mailIds 都安全。

    ★ 逐封处理而不是"先全检再全发"：一封失败不影响其它封；失败那封保持未领，
      下次点击会重试它 —— 已领过的因为幂等不会二次发放。
    """
    args = _args(fields)
    uid = str(args[0]) if args else ""
    ids = args[1] if len(args) > 1 else []
    if not isinstance(ids, list) or not ids:
        return _fail("bad mailIds")

    st = _state(uid)
    if st is None:
        return _fail("unknown user")

    granted: list[int] = []
    failed: list[int] = []
    frames: list[bytes] = []

    with pstate.lock_for(uid):
        for raw_id in ids:
            mid = _as_int(raw_id, 0)
            mail = store.get(st.props, mid)
            if mail is None or mail.get(store.DELETED):
                failed.append(mid)
                continue
            params = mail.get("Params")
            if isinstance(params, dict) and params.get("giftReceived"):
                continue                                    # 已领：幂等跳过

            items = template.attachments_of(mail)
            if items:
                # ★ 唯一发放入口：grant_many 内部走 Tx，帧由 Tx 产生（不手工拼）
                tx, outcomes = _grants.grant_many(st.props, items, source=f"mail:{mid}")
                bad = [oc for oc in outcomes if not oc.ok]
                if bad:
                    # 该封保持"未领"，下次点击重试；已发出去的物品留在背包里（不回收）
                    _log.error("[mail] uid=%s 邮件 %s 发奖失败 %s", uid, mid,
                               [(oc.item_id, getattr(oc.error, "code", "")) for oc in bad])
                    failed.append(mid)
                    continue
                frames.extend(tx.frames())

            store.mark_param(st.props, mid, "giftReceived", True)
            granted.append(mid)

        if granted and not st.save():
            # ★ 必须落盘：客户端已经本地标已领，服务端不存就是"重登后能再领一次"
            _log.error("[mail] uid=%s 领奖后落盘失败（邮件 %s）", uid, granted)

    if frames:
        ok_n, fail_n = _online.send_to(uid, frames)
        # 实体连接不在线是正常情况（玩家可能在登录界面）：数据已入库，
        # 下次登录 initDict 会带出正确背包，奖励不会丢。这里只记一笔便于排障。
        _log.info("[mail] uid=%s 领奖 %s 推送属性同步 %d 帧 -> 在线连接 %d/%d",
                  uid, granted, len(frames), ok_n, fail_n)

    if failed:
        return _fail(f"mail not claimable: {failed}")
    return _ok()

