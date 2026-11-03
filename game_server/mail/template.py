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


def build(template_id: int, *, created: int, custom: dict | None = None) -> dict[str, Any]:
    """造一封邮件实例（不含 MailId —— 由 store.add 分配）。

    返回的 dict 就是客户端契约里的形状：
        Title / SrcName / CreatedTime / ExpireTime / Params{...}

    ★ 实例会**快照** Title / SrcName / Content：模板以后改了，老邮件不该跟着变。

    ## custom：自定义覆盖（2026-11-03）

    这是**唯一**的造邮件入口，模板邮件和完全自定义的邮件都走它 ——
    不新开第二条路径（否则两个地方各自拼字段，迟早不一致）。

    custom 的键：
        title    str    覆盖标题
        sender   str    覆盖发件人
        content  str    覆盖正文**并快照进实例**（不从模板取）
        items    dict   {item_id: count}，覆盖附件；传 {} 表示"明确没有附件"
        link_text / link str  覆盖链接

    ★ template_id 可以是 0（纯自定义邮件）。此时客户端按 mailTemplateId=0 查
      DataLoader:getMailData()[0] 得到 nil，走 `if not mailData then return end`
      的兜底：没有头像、没有跳转，其余照常显示（avatarcomp/mailpanel 都验过）。
    """
    tid = int(template_id)
    tpl = template(tid) or {}
    custom = custom or {}

    # ---------------------------------------------------------------- 附件
    if "items" in custom:
        items: dict[int, int] = dict(custom.get("items") or {})
    else:
        items = attachment_items(tpl)

    params: dict[str, Any] = {
        "mailTemplateId": tid,
        "haveRead": False,
        "star": False,
    }
    if items:
        # ★ 必须是 JSON 字符串（客户端 json.decode(mail.Params.giftInfo)），
        #   且键是数字字符串 —— 展示走 pairs + tonumber(id)。
        params["giftInfo"] = gift_info_json(items)
    # 附件状态总是显式给出：客户端多处是 `not Params.giftReceived` / `== false` 判。
    params["giftReceived"] = False

    # 链接：custom 优先，其次模板；空串等于没有（客户端判的是 linkText ~= nil）
    links = {
        "linkText": custom.get("link_text", tpl.get("linkText")),
        "link": custom.get("link", tpl.get("link")),
    }
    params.update({k: v for k, v in links.items() if isinstance(v, str) and v})

    content = custom.get("content")
    mail: dict[str, Any] = {
        "Title": str(custom.get("title") or tpl.get("title") or ""),
        "SrcName": str(custom.get("sender") or tpl.get("sender") or ""),
        "CreatedTime": int(created),
        # 有效期统一走 DEFAULT_VALID_SECONDS（理由见模块头 U2）。
        # ★ 模板里的 validTime 现在**不参与计算** —— 单位没取证，猜错会让邮件立刻过期。
        "ExpireTime": int(created) + DEFAULT_VALID_SECONDS,
        "Params": params,
    }
    if isinstance(content, str):
        # ★ 自定义正文**快照进实例**：没有这一条，content_of 只能回模板取，
        #   自定义的文案就没地方存（这是 content_of 那套"不存正文"设计的唯一例外）。
        mail["Content"] = content
    return mail


def content_of(mail: dict | None) -> str:
    """取正文。

    优先级：
      1. 实例里的 Content（自定义邮件快照）—— 有就用，绝不回模板覆盖
      2. 按 Params.mailTemplateId 回 BDD 取（187 条内置模板走这条）
      3. 都没有 -> 空串

    ★ 第 1 条优先是必须的：自定义邮件如果还去查模板，玩家看到的就是别人的文案。
    """
    if not isinstance(mail, dict):
        return ""
    snap = mail.get("Content")
    if isinstance(snap, str):
        return snap
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
