#!/usr/bin/env python3
"""离线发信工具（tools/mail_send.py）测试。

用法:
    python tests/test_mail_tool.py

为什么工具也要测：实机验收（面板能不能打开、附件能不能领）靠它造数据，
它一旦坏了，人会以为是游戏功能坏了。

被测的不变量：

    INV-TOOL-1  发信走 send_mail 唯一入口（产物是完整模板实例，不是手拼 dict）
    INV-TOOL-2  模板不存在时**不写存档**（不许留下半截空状态）
    INV-TOOL-3  发完立刻落盘，另一个进程能读回来（实机验收的前提）
    INV-TOOL-4  列出模板只列"带附件的"，且按 id 排序
"""
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_mailtool_")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")

from game_server import player_state as pstate  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from tools import mail_send  # noqa: E402

UID = "tool_u"
TPL = 1
passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData")
        return 0

    # ------------------------------------------------------------ INV-TOOL-1
    print("\n[1] INV-TOOL-1 发一封：产物是完整模板实例")
    sent = mail_send.send_to(UID, TPL, 2)
    check("返回 2 个实例", len(sent) == 2, len(sent))
    check("MailId 递增", [m["MailId"] for m in sent] == [1, 2], [m["MailId"] for m in sent])
    tpl = template.template(TPL)
    check("Title 来自模板", sent[0].get("Title") == tpl.get("title"), sent[0].get("Title"))
    check("ExpireTime > 0", int(sent[0].get("ExpireTime") or 0) > 0)
    check("带模板附件", template.attachments_of(sent[0]) != {})

    # ------------------------------------------------------------ INV-TOOL-3
    print("\n[2] INV-TOOL-3 落盘：重新读盘（等价于另一个进程）能读回来")
    pstate.STATES.pop(UID, None)
    back = mail_send.list_mails(UID)
    check("重新读盘拿到 2 封", back is not None and len(back) == 2,
          None if back is None else len(back))
    check("按时间倒序（新邮件在前）", back and int(back[0]["MailId"]) == 2,
          back[0]["MailId"] if back else None)

    # ------------------------------------------------------------ INV-TOOL-2
    print("\n[3] INV-TOOL-2 模板不存在：不写存档、不留半截状态")
    nobody = "tool_none"
    path = os.path.join(os.environ["OO_STATE_DIR"], nobody + ".json")
    if os.path.exists(path):
        os.remove(path)
    got = mail_send.send_to(nobody, 987654321)
    check("返回空列表", got == [], got)
    check("没有写出存档文件", not os.path.exists(path))
    check("list_mails 对没有存档的账号返回 None", mail_send.list_mails(nobody) is None)

    # 存档写出后再发失败，不许破坏原有邮箱
    before = mail_send.list_mails(UID)
    mail_send.send_to(UID, 987654321)
    after = mail_send.list_mails(UID)
    check("失败的发信不影响已有邮件", len(after) == len(before), (len(before), len(after)))

    # ------------------------------------------------------------ INV-TOOL-4
    print("\n[4] INV-TOOL-4 列出模板：只列带附件的，按 id 排序")
    rows = mail_send.list_templates(10)
    check("能列出模板", len(rows) > 0, len(rows))
    check("每条都有附件", all(items for _tid, _t, items in rows))
    ids = [tid for tid, _t, _i in rows]
    check("按 id 升序", ids == sorted(ids), ids)
    check("模板 1 在列表里（它确实有附件）", TPL in ids, ids[:5])

    # ------------------------------------------------------------ 存储形状
    print("\n[5] 存档里存的是 _oo_mail，不进 initDict 的口径")
    raw = json.load(open(path.replace(nobody, UID), encoding="utf-8"))
    check("存档里有 _oo_mail", store.MAIL_KEY in raw.get("props", {}), list(raw.get("props", {}))[:5])
    check("_oo_mail 里有 mails", isinstance(raw["props"][store.MAIL_KEY].get("mails"), dict))

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
