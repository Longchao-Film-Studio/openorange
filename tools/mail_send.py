#!/usr/bin/env python3
"""给玩家发邮件（离线改存档）。

用法
----
    python tools/mail_send.py --uid 15900159000 --template 1
    python tools/mail_send.py --uid 15900159000 --template 1 --count 3
    python tools/mail_send.py --uid 15900159000 --list
    python tools/mail_send.py --templates 20          # 看看有哪些模板可发
    python tools/mail_send.py --uid ... --template 1 --dry-run

说明
----
- 有存档就基于存档改；没有就从 game_server/avatar_init.json 起一份；
- **发信走 mail.send.send_mail 唯一入口**（模板 → 实例 → 存储），本工具不拼字段；
- 写回 work/player_state/<uid>.json；
- **需要重新登录游戏才生效**（邮件是客户端登录后主动拉的，服务端重启也会从存档读）。

为什么需要它
------------
游戏内 GM 命令（GmRpcSendMailToPlayer）要先进游戏才能用；实机验收"面板能不能打开、
附件能不能领"时往往还没法进去。离线发信是当前最可靠的方式。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.write_guard import write_guard  # noqa: E402

INIT_JSON = os.path.join(_ROOT, "game_server", "avatar_init.json")


def load_state(uid):
    """取玩家状态：优先存档，否则用 initDict 起一份。

    ★ props["uid"] 必须覆盖成 uid：从 initDict 起状态时它还是模板里的默认值，
      而客户端读的正是这个字段（以前 give_item.py 漏过，存档里出现两个 uid）。
    """
    st = pstate.load(uid)
    if st is None:
        with open(INIT_JSON, encoding="utf-8") as f:
            d = json.load(f)
        st = pstate.PlayerState(uid=uid, props=msmod.conv_int_keys(d["__Properties__"]))
    if st.props.get("uid") != uid:
        with write_guard("tool:mail_send"):
            st.props["uid"] = uid
    return st


def send_to(uid, template_id, count=1):
    """给 uid 发 count 封 template_id 邮件，返回新邮件实例列表（空 = 模板不存在）。"""
    st = load_state(uid)
    sent = mailsend.send_mail(st, int(template_id), count=int(count))
    if sent:
        st.save()
    return sent


def list_mails(uid):
    """玩家邮箱里的邮件（未删除的）。玩家不存在返回 None。"""
    st = pstate.load(uid)
    if st is None:
        return None
    return store.all_mails(st.props)


def list_templates(limit=20):
    """BDD 里可发的模板（只挑有附件的，最像"真正的奖励邮件"）。"""
    rows = []
    table = gamedata.table("MailData") or {}
    for key, row in table.items():
        if not isinstance(row, dict):
            continue
        items = template.attachment_items(row)
        if not items:
            continue
        rows.append((int(row.get("mailId") or key), row.get("title") or "", items))
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r[0])
    return rows


def _fmt_ts(ts):
    import time as _t
    try:
        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def main(argv=None):
    ap = argparse.ArgumentParser(description="给玩家发邮件（离线改存档）")
    ap.add_argument("--uid", help="玩家账号名（= 游戏内 UID）")
    ap.add_argument("--template", type=int, help="BDD MailData 的模板 id")
    ap.add_argument("--count", type=int, default=1, help="发几封（默认 1）")
    ap.add_argument("--list", action="store_true", help="列出这个玩家的邮箱")
    ap.add_argument("--templates", type=int, nargs="?", const=20, default=None,
                    help="列出可发的模板（默认 20 条，只列带附件的）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args(argv)

    if args.templates is not None:
        rows = list_templates(max(1, args.templates))
        if not rows:
            print("BDD MailData 里没有带附件的模板（检查 OO_BDD_DIR）")
            return 1
        print(f"{'模板':>8}  {'附件':<28} 标题")
        for tid, title, items in rows:
            att = ", ".join(f"{k}x{v}" for k, v in sorted(items.items()))
            print(f"{tid:>8}  {att:<28} {title}")
        return 0

    if not args.uid:
        ap.error("要么给 --uid，要么用 --templates 看模板")

    if args.list:
        mails = list_mails(args.uid)
        if mails is None:
            print(f"没有这个账号的存档：{args.uid}")
            return 1
        if not mails:
            print(f"{args.uid} 的邮箱是空的")
            return 0
        print(f"{args.uid} 的邮箱（{len(mails)} 封）：")
        for m in mails:
            params = m.get("Params") or {}
            got = "已领" if params.get("giftReceived") else "未领"
            read = "已读" if params.get("haveRead") else "未读"
            print(f"  #{m.get('MailId'):<5} {got}/{read}  {m.get('Title', '')}  "
                  f"（{_fmt_ts(m.get('CreatedTime'))} 到期 {_fmt_ts(m.get('ExpireTime'))}）")
        return 0

    if args.template is None:
        ap.error("要发信必须给 --template（先用 --templates 看有哪些）")

    if template.template(args.template) is None:
        print(f"模板 {args.template} 不在 BDD MailData 里（先用 --templates 看有哪些）")
        return 1

    if args.dry_run:
        tpl = template.template(args.template)
        print(f"[dry-run] 会给 {args.uid} 发 {args.count} 封模板 {args.template}"
              f"（{tpl.get('title', '')}），不写盘")
        return 0

    sent = send_to(args.uid, args.template, args.count)
    if not sent:
        print("发信失败（模板不存在或存档不可写）")
        return 1
    print(f"已给 {args.uid} 发 {len(sent)} 封，MailId = {[m['MailId'] for m in sent]}")
    print("重新登录游戏后打开邮件面板即可看到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
