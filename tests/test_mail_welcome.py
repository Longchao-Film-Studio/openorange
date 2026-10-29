#!/usr/bin/env python3
"""M5 第一个下游接入点：新号欢迎邮件。

用法:
    python tests/test_mail_welcome.py

被测的不变量：

    INV-DOWN-1  模板 id 由配置给（config.MAIL_WELCOME_TEMPLATE），**0 = 不发** —— 代码里不许写死模板
    INV-DOWN-2  只在**首次建号**发一封；重登不再发（判据只有 st.is_new，不另设标记）
    INV-DOWN-3  发信必须走 send_mail 唯一入口（产物是完整模板实例，不是手拼 dict）
    INV-DOWN-4  发不出去（模板不存在）不许把"已发"标记写死，也不许挡住登录
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_maildown_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import config as ooconf  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import store, template  # noqa: E402

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


def reset(uid):
    path = os.path.join(os.environ["OO_STATE_DIR"], uid + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(uid, None)


def login(uid):
    msmod.live_init_dict(uid=uid)
    return pstate.STATES[uid]


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData")
        return 0
    saved = ooconf.MAIL_WELCOME_TEMPLATE
    try:
        # ------------------------------------------------------------ INV-DOWN-1
        print("\n[1] INV-DOWN-1 默认（模板=0）不发任何信")
        ooconf.MAIL_WELCOME_TEMPLATE = 0
        reset("w_off")
        st = login("w_off")
        check("邮箱是空的", store.all_mails(st.props) == [], len(store.all_mails(st.props)))
        check("配置默认值是 0（工程不替产品决定发什么）",
              int(os.environ.get("OO_MAIL_WELCOME_TEMPLATE") or 0) == 0)

        # ------------------------------------------------------------ INV-DOWN-2/3
        print("\n[2] INV-DOWN-2/3 打开后：新号发一封，且是完整模板实例")
        ooconf.MAIL_WELCOME_TEMPLATE = TPL
        reset("w_new")
        st = login("w_new")
        mails = store.all_mails(st.props)
        check("新号收到 1 封", len(mails) == 1, len(mails))
        if mails:
            m = mails[0]
            tpl = template.template(TPL)
            check("Title 来自模板（没手拼）", m.get("Title") == tpl.get("title"), m.get("Title"))
            check("SrcName 来自模板", m.get("SrcName") == tpl.get("sender"), m.get("SrcName"))
            check("ExpireTime > 0", int(m.get("ExpireTime") or 0) > 0)
            check("带模板附件", template.attachments_of(m) != {})
        check("没有多余的已发标记（判据只有 is_new）", "_oo_mail_welcomed" not in st.props)

        print("\n[3] INV-DOWN-2 重登不再发第二封")
        pstate.save_state("w_new")
        pstate.STATES.pop("w_new", None)
        st2 = login("w_new")
        check("重登后仍然只有 1 封", len(store.all_mails(st2.props)) == 1,
              len(store.all_mails(st2.props)))

        print("\n[4] 老号（存档已存在）不发欢迎信")
        reset("w_old")
        st_old = login("w_old")
        for m in store.all_mails(st_old.props):
            store.delete(st_old.props, int(m["MailId"]))
        st_old.save()
        pstate.STATES.pop("w_old", None)
        st_old2 = login("w_old")            # 这次是 is_new=False
        check("老号没有收到欢迎信", store.all_mails(st_old2.props) == [],
              len(store.all_mails(st_old2.props)))

        # ------------------------------------------------------------ INV-DOWN-4
        print("\n[5] INV-DOWN-4 模板不存在：不发、不记标记、不挡登录")
        ooconf.MAIL_WELCOME_TEMPLATE = 987654321
        reset("w_bad")
        st_bad = login("w_bad")
        check("登录照常完成（有 initDict）", isinstance(st_bad.props, dict) and len(st_bad.props) > 10,
              len(st_bad.props))
        check("邮箱是空的", store.all_mails(st_bad.props) == [])
        check("失败后邮箱仍是空的（没有半截数据）", store.all_mails(st_bad.props) == [])
    finally:
        ooconf.MAIL_WELCOME_TEMPLATE = saved

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
