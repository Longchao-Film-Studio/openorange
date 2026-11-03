#!/usr/bin/env python3
"""发信：**服务端唯一发信入口**。

以后所有"给玩家发邮件"的系统（GM 命令、副本结算、排行榜奖励、战令补发、
活动奖励、体力溢出转邮件）都从这里走，不许各自拼 dict —— 那样每加一个
发信点就多一处字段契约，迟早有一处写错（ExpireTime=0 或 giftInfo 不是字符串）。

    发送方                本模块
    ─────────────────────────────────────────
    GM 命令     ─┐
    副本结算     ├─> send_mail(st, template_id, ...) ─> store.add
    排行榜奖励   │                                        ↓
    战令补发    ─┘                                   props["_oo_mail"]

## 为什么需要 state 而不是 uid

发信必须改 props，而 props 属于 PlayerState。传 st 而不是 uid 是为了强制调用方
先把状态取出来（也就强制它持有 `player_state.lock_for(uid)` 的那把锁）。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import push as _push
from . import store, template

if TYPE_CHECKING:
    from ..player_state import PlayerState


def send_mail(st: PlayerState, template_id: int, *, count: int = 1,
              now: int | None = None, custom: dict | None = None) -> list[dict]:
    """给 st 对应的玩家发 count 封 template_id 邮件，返回实例列表。

    调用约定：
      * 必须已经持有该账号的锁（`with pstate.lock_for(uid):`）；
      * 发完由调用方负责落盘（`pstate.save_state(uid)` 或节流保存）；
      * `now` 只给测试用，正常运行不要传。

    count 的语义是"同样的信发几封"（例如排行奖励按档位重复），不是"一封里放几份附件"。
    模板不存在 -> 返回空列表（不抛：发信不该让调用方的业务崩），但会记 warning。
    """
    from shared.log_tee import get_logger  # noqa: PLC0415

    _log = get_logger(__name__)
    ts = int(now if now is not None else time.time())
    tid = int(template_id)
    tpl = template.template(tid)
    if tpl is None and not custom:
        _log.warning("[mail] 模板 %s 不存在（BDD MailData），发信跳过", tid)
        return []
    if tpl is None:
        _log.info("[mail] 模板 %s 不存在，按纯自定义邮件发送（无头像/无跳转）", tid)
    out: list[dict] = []
    for _ in range(max(1, int(count))):
        # 自定义正文/附件都不该被多封共享同一个可变对象，build 每次都造新 dict。
        mail = template.build(tid, created=ts, custom=custom)
        out.append(store.add(st.props, mail, now=ts))
    # ★ 推送放在唯一入口里：GM 发信 / 新号欢迎 / 以后的所有下游接入都自动获得推送，
    #   不需要每个发信方各自记得调一次（那正是"同一件事写五遍"的开端）。
    #   不在线时返回 False，不是错误：邮件已落库，下次登录拉列表可见。
    _push.notify_new_mail(st, out)
    return out
