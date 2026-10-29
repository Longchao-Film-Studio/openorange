#!/usr/bin/env python3
"""邮件管理（M3）测试：已读 / 星标 / 删除。

用法:
    python tests/test_mail_manage.py

被测的不变量：

    INV-MGMT-1  这三个方法改的都是**邮件自身状态**，不是玩家属性 ——
                落盘即可，不许产生属性同步帧（产生帧 = 客户端收到不存在的属性）
    INV-MGMT-2  全部幂等：重复已读 / 重复星标 / 重复删除都回成功
    INV-MGMT-3  状态必须落盘（重登后仍然生效）
    INV-MGMT-4  deleteMail 是**软删**：数据还在，只是列表里不见了
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_mailmgmt_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.ms import registry  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402

UID = "mail_mgmt_u"
TPL = 1
ALL_METHODS = (
    "getUserMailList", "getMailContent", "setMailHaveRead", "setAllMailHaveRead",
    "setMailGiftReceived", "deleteMail", "star", "unstar",
    "syncGroupMail", "queryNotification", "readNotification",
)

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


def fresh(uid=UID):
    path = os.path.join(os.environ["OO_STATE_DIR"], uid + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(uid, None)
    msmod.live_init_dict(uid=uid)
    return pstate.STATES[uid]


def call(method, *args, uid=UID):
    fields = {4: [msmod.mp_arr([uid, *args])]}
    return msmod.mp_decode(svc.ms_reply_params("MailService", method, fields, 0))[0]


def ok(r):
    return isinstance(r, dict) and isinstance(r.get("Ret"), list) and r["Ret"][0] is True


def send(st, n=1):
    return [mailsend.send_mail(st, TPL)[0]["MailId"] for _ in range(n)]


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData")
        return 0

    # ------------------------------------------------------------ 全量登记
    print("\n[1] 11 个方法必须全部登记（否则客户端某个按钮必然没反应）")
    svc._ensure_handlers_loaded()
    got = sorted(m for (s, m) in registry.table() if s == "MailService")
    check("11 个方法全部登记", got == sorted(ALL_METHODS), got)

    # ------------------------------------------------------------ 星标
    print("\n[2] 星标：置位 / 取消 / 落盘")
    st = fresh()
    mid = send(st)[0]
    check("初始未加星", store.get(st.props, mid)["Params"]["star"] is False)
    check("star 回成功", ok(call("star", mid)))
    check("已加星", store.get(st.props, mid)["Params"]["star"] is True)
    check("重复 star 仍成功（幂等）", ok(call("star", mid)))
    check("unstar 回成功", ok(call("unstar", mid)))
    check("星标已取消", store.get(st.props, mid)["Params"]["star"] is False)

    pstate.save_state(UID)
    st_reload = pstate.load(UID)
    check("重登后星标状态仍在（落盘）", st_reload is not None
          and st_reload.props[store.MAIL_KEY]["mails"][str(mid)]["Params"]["star"] is False)

    # ------------------------------------------------------------ 已读
    print("\n[3] 已读：单封 / 全部 / 不存在的邮件")
    st = fresh()
    a, b, c = send(st, 3)
    check("初始都未读",
          all(store.get(st.props, i)["Params"]["haveRead"] is False for i in (a, b, c)))
    check("setMailHaveRead 回成功", ok(call("setMailHaveRead", a)))
    check(f"只 {a} 变成已读", store.get(st.props, a)["Params"]["haveRead"] is True)
    check("其它两封仍未读",
          all(store.get(st.props, i)["Params"]["haveRead"] is False for i in (b, c)))
    check("重复已读仍成功（幂等）", ok(call("setMailHaveRead", a)))
    check("不存在的邮件 -> fail", not ok(call("setMailHaveRead", 999999)))

    check("setAllMailHaveRead 回成功", ok(call("setAllMailHaveRead")))
    check("三封全部已读",
          all(store.get(st.props, i)["Params"]["haveRead"] is True for i in (a, b, c)))
    check("清掉了新邮件标记（看过 = 不该再亮红点）", store.has_new(st.props) is False)

    # ------------------------------------------------------------ 删除
    print("\n[4] 删除：软删 / 幂等 / 列表里消失")
    st = fresh()
    x, y = send(st, 2)
    check("删前两封都在", len(store.all_mails(st.props)) == 2)
    check("deleteMail 回成功", ok(call("deleteMail", [x])))
    check("列表少了一封", [m["MailId"] for m in store.all_mails(st.props)] == [y])
    check("数据还在（软删，INV-MGMT-4）", store.get(st.props, x) is not None)
    check("软删标记为真", store.get(st.props, x).get(store.DELETED) is True)
    check("重复删除仍成功（幂等）", ok(call("deleteMail", [x])))
    check("删不存在的也回成功（幂等，避免界面卡住）", ok(call("deleteMail", [999999])))
    check("空 id 列表 -> fail", not ok(call("deleteMail", [])))

    pstate.save_state(UID)
    st_reload = pstate.load(UID)
    check("重登后仍只有一封可见", st_reload is not None
          and len(store.all_mails(st_reload.props)) == 1)

    # ------------------------------------------------------------ 不产生帧
    print("\n[5] INV-MGMT-1 这三个方法不该产生属性同步帧")
    from game_server.transport import online as _online
    sent = []
    orig = _online.send_to
    def _fake_send_to(_uid, _frames):
        sent.append(1)
        return (1, 0)

    _online.send_to = _fake_send_to
    try:
        st = fresh()
        m = send(st)[0]
        call("star", m)
        call("unstar", m)
        call("setMailHaveRead", m)
        call("deleteMail", [m])
    finally:
        _online.send_to = orig
    check("一次都没推属性同步", sent == [], len(sent))

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
