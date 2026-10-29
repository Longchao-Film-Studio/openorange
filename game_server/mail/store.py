#!/usr/bin/env python3
"""邮件箱：服务端私有存储。**唯一写入口只有三个**（add / mark / delete）。

## 为什么存在 props["_oo_mail"]，而不是新根属性

客户端 `initProperty`（core/propertysync/puppetpropertycomponent.lua:38-66）对**表值**
要求 `__tp__` 指向一个已注册的自定义类型，否则 `assert` 或丢弃。
客户端 345 个自定义类型里**没有任何邮件相关类型**（实测），所以"新建一个根属性放邮件"
会让客户端在登录时崩，而不是"多一个属性"。

`_oo_` 前缀正好同时满足三条：

    不进 initDict        ms/initdict.py 的 _stage_public_snapshot 逐 key 跳过 _oo_
    不参与属性契约        prop_origin.all_props() 显式排除 _oo_
    但**要**写盘          prop_origin.filter_for_save 对 _oo_ 保留

## 数据形状

    props["_oo_mail"] = {
        "next_id":  1003,              # 实例 id 自增（与模板 id 无关）
        "mails":    {"1001": {...}},   # MailId -> 邮件实例
        "new_mark": True,              # 有没有"新邮件"待通知（queryNotification 用）
    }

邮件实例的字段必须严格按客户端契约（见 docs/方案-邮件系统-全链路复检.md §4.2）：

    MailId / Title / SrcName / CreatedTime / ExpireTime / Params{...}

★ ExpireTime **必须 > 0 且 > 当前时间**。客户端 `getUnexpiredMailKeyList`
  （avatarmailcomp.lua:59-68）用 `ExpireTime > now` 过滤，`ExpireTime == 0` 的邮件
  在面板里**根本不会出现**（而红点逻辑又把它算作未读 —— 会变成"红点亮着但看不到邮件"）。

## 并发

本模块只碰 props，不碰 socket。写入口自己开 `write_guard`；
账号级串行由调用方用 `player_state.lock_for(uid)` 保证（与实体 RPC 同一把锁）。
"""
from __future__ import annotations

from typing import Any

from ..write_guard import write_guard

#: props 里的键名。改它需要写迁移（目前没有历史数据，所以直接换）。
MAIL_KEY = "_oo_mail"

#: 已删除邮件的软标记字段（不物理删除：过期/删掉的邮件出问题时还要能查）。
DELETED = "Deleted"


# ------------------------------------------------------------------ 读
def _root(props: dict) -> dict | None:
    v = props.get(MAIL_KEY)
    return v if isinstance(v, dict) else None


def box(props: dict) -> dict[str, dict]:
    """{MailId(str): 实例}。没有邮件箱时返回空 dict（不创建）。"""
    root = _root(props)
    if root is None:
        return {}
    mails = root.get("mails")
    return mails if isinstance(mails, dict) else {}


def get(props: dict, mail_id: int) -> dict | None:
    m = box(props).get(str(int(mail_id)))
    return m if isinstance(m, dict) else None


def has_new(props: dict) -> bool:
    root = _root(props)
    return bool(root and root.get("new_mark"))


def all_mails(props: dict) -> list[dict]:
    """全部**未删除**的邮件，按 (CreatedTime, MailId) 倒序 —— 新邮件在前。"""
    items = [m for m in box(props).values()
             if isinstance(m, dict) and not m.get(DELETED)]
    items.sort(key=lambda m: (int(m.get("CreatedTime") or 0), int(m.get("MailId") or 0)),
               reverse=True)
    return items


# ------------------------------------------------------------------ 写（只有三个）
def _ensure(props: dict) -> dict:
    root = _root(props)
    if root is None:
        root = {"next_id": 0, "mails": {}, "new_mark": False}
        props[MAIL_KEY] = root
    root.setdefault("next_id", 0)
    if not isinstance(root.get("mails"), dict):
        root["mails"] = {}
    root.setdefault("new_mark", False)
    return root


def next_id(props: dict) -> int:
    """下一个实例 id（只读，不消耗）。"""
    root = _root(props)
    return int(root.get("next_id") or 0) + 1 if root else 1


def add(props: dict, mail: dict, *, now: int | None = None) -> dict:
    """写入一封邮件并返回它（MailId 在这里分配）。

    mail 必须已含 Title / SrcName / Params；CreatedTime / ExpireTime 缺了会按 now 补齐。
    `now` 传进来是为了可测（别在这里读 time.time()）。
    """
    import time as _time  # noqa: PLC0415

    ts = int(now if now is not None else _time.time())
    with write_guard("mail.store.add"):
        root = _ensure(props)
        mid = int(root["next_id"]) + 1
        root["next_id"] = mid
        record = dict(mail)
        record["MailId"] = mid
        record.setdefault("CreatedTime", ts)
        if not record.get("ExpireTime"):
            # 兜底：绝不允许 ExpireTime 为 0（客户端会看不见这封邮件）
            record["ExpireTime"] = ts + 30 * 86400
        root["mails"][str(mid)] = record
        root["new_mark"] = True
        return record


def mark(props: dict, mail_id: int, **fields: Any) -> bool:  # noqa: ANN401
    """改一封邮件的字段（giftReceived / haveRead / star …）。不存在返回 False。"""
    with write_guard("mail.store.mark"):
        rec = get(props, mail_id)
        if rec is None:
            return False
        for k, v in fields.items():
            if k in ("MailId", "CreatedTime"):
                raise ValueError(f"邮件字段 {k} 不允许改")
            rec[k] = v
        return True


def mark_param(props: dict, mail_id: int, key: str, value: Any) -> bool:  # noqa: ANN401
    """改 Params 里的一个字段（客户端读的都是 Params.xxx）。"""
    with write_guard("mail.store.mark_param"):
        rec = get(props, mail_id)
        if rec is None:
            return False
        params = rec.get("Params")
        if not isinstance(params, dict):
            params = {}
            rec["Params"] = params
        params[key] = value
        return True


def delete(props: dict, mail_id: int) -> bool:
    """软删除（只打标记，不物理删）。已删过返回 False。"""
    with write_guard("mail.store.delete"):
        rec = get(props, mail_id)
        if rec is None or rec.get(DELETED):
            return False
        rec[DELETED] = True
        return True


def clear_new(props: dict) -> bool:
    """清掉"新邮件"标记（客户端 readNotification 调用）。返回之前有没有。"""
    with write_guard("mail.store.clear_new"):
        root = _root(props)
        if not root or not root.get("new_mark"):
            return False
        root["new_mark"] = False
        return True
