#!/usr/bin/env python3
"""邮件模板（BDD MailData）-> 邮件实例。

## 配置来源

BDD `TableData.CommonData.MailData.json`：187 条模板，96 条带附件（实测）。
字段（出现次数/187）：mailId title sender content 全量、validTime 186、
attachedItem 96、defaultTag 5、linkText/link 各 3、headIcon 2、expireTime/jumpText/JumpTo 各 1。

★ **判据只能是 BDD**：客户端自己那张 `tabledata/commondata/maildata.lua` 只有 85 条，
  是 BDD 的严格子集（缺 102 条，且整张表没有 JumpTo）。发一封"客户端表里没有"的模板
  不会崩（`if not mailData then return end`），只是列表里显示系统图标、没有跳转。

## 有效期

★★ `validTime` 的**单位未取证**（U2：可能是小时，也可能是别的）。在取证之前**不许猜**，
   统一用 `DEFAULT_VALID_SECONDS`（30 天）占位，并且只在这里一个地方定义 ——
   将来确认单位后改这一个函数即可，不散落在业务里。

   注意不能"先用 validTime 当秒"：那会让 validTime=1 的模板变成 1 秒过期，
   玩家点开面板时邮件已经消失了。

## 附件 -> giftInfo

客户端 `isHasMailAward`（avatarmailcomp.lua:117-129）读 `json.decode(mail.Params.giftInfo)`，
所以 `giftInfo` 必须是**字符串形式的 JSON 对象** `{"103":1000}`（键是数字字符串）。
它用 `table.length(award) > 0` 判非空 —— 那个函数数的是 pairs 数量（不是 #），
所以对象形式和数组形式都能过；但展示走 `pairs` + `tonumber(id)`，键必须是数字字符串。
"""
from __future__ import annotations

import json
from typing import Any

from .. import gamedata

#: 占位有效期（30 天）。U2 取证后按模板的 validTime 改这里。
DEFAULT_VALID_SECONDS = 30 * 86400


def template(template_id: int) -> dict | None:
    """BDD MailData[template_id]。"""
    tid = int(template_id)
    d = gamedata.table("MailData")
    if not isinstance(d, dict):
        return None
    row = d.get(str(tid)) or d.get(tid)
    return row if isinstance(row, dict) else None


def attachment_items(tpl: dict | None) -> dict[int, int]:
    """模板附件 -> {item_id: count}。没有附件返回空 dict。

    BDD 的 attachedItem 是 OrderedDict：`{"103": 1000, "KeyList": [103], "__bdd_metatype__": ...}`。
    只取 KeyList 里列出的键，避免把元数据键当道具。
    """
    if not isinstance(tpl, dict):
        return {}
    raw = tpl.get("attachedItem")
    if not isinstance(raw, dict):
        return {}
    keys = raw.get("KeyList")
    if not isinstance(keys, list):
        keys = [k for k in raw if str(k).lstrip("-").isdigit()]
    out: dict[int, int] = {}
    for k in keys:
        try:
            iid = int(k)
        except (TypeError, ValueError):
            continue
        cnt = raw.get(str(iid))
        try:
            n = int(cnt) if cnt is not None else 0
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            out[iid] = n
    return out


def gift_info_json(items: dict[int, int]) -> str:
    """{item_id: count} -> 客户端要的 JSON 字符串（键按 id 排序，输出稳定）。"""
    return json.dumps({str(k): int(v) for k, v in sorted(items.items())},
                      separators=(",", ":"), ensure_ascii=False)


def build(template_id: int, *, created: int) -> dict[str, Any]:
    """按模板造一封邮件实例（不含 MailId —— 由 store.add 分配）。

    返回的 dict 就是客户端契约里的形状：
        Title / SrcName / CreatedTime / ExpireTime / Params{...}

    ★ 实例会**快照** Title 与 SrcName：模板以后改了，老邮件不该跟着变。
      正文不存（客户端每封都要单独 getMailContent，那时再从 BDD 取即可），省存档空间。
    """
    tpl = template(template_id) or {}
    params: dict[str, Any] = {
        "mailTemplateId": int(template_id),
        "haveRead": False,
        "star": False,
    }
    items = attachment_items(tpl)
    if items:
        params["giftInfo"] = gift_info_json(items)
    # 附件状态总是显式给出：客户端多处是 `not Params.giftReceived` / `== false` 判。
    params["giftReceived"] = False
    for k in ("linkText", "link"):
        v = tpl.get(k)
        if isinstance(v, str) and v:
            params[k] = v
    return {
        "Title": str(tpl.get("title") or ""),
        "SrcName": str(tpl.get("sender") or ""),
        "CreatedTime": int(created),
        # 有效期统一走 DEFAULT_VALID_SECONDS（理由见模块头 U2）。
        # ★ 模板里的 validTime 现在**不参与计算** —— 单位没取证，猜错会让邮件立刻过期。
        "ExpireTime": int(created) + DEFAULT_VALID_SECONDS,
        "Params": params,
    }


def content_of(mail: dict | None) -> str:
    """取正文：按实例里的 Params.mailTemplateId 回 BDD 拿。取不到返回空串。"""
    if not isinstance(mail, dict):
        return ""
    params = mail.get("Params")
    tid = params.get("mailTemplateId") if isinstance(params, dict) else None
    if tid is None:
        return ""
    tpl = template(int(tid)) or {}
    v = tpl.get("content")
    return v if isinstance(v, str) else ""


def attachments_of(mail: dict | None) -> dict[int, int]:
    """从实例的 Params.giftInfo 反解 {item_id: count}（领奖时用）。"""
    if not isinstance(mail, dict):
        return {}
    params = mail.get("Params")
    raw = params.get("giftInfo") if isinstance(params, dict) else None
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict[int, int] = {}
    for k, v in d.items():
        try:
            iid = int(k)
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[iid] = n
    return out
