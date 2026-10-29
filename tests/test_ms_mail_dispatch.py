#!/usr/bin/env python3
"""微服务分派测试：一个方法名只能属于一个服务。

用法:
    python tests/test_ms_mail_dispatch.py

被测的不变量：

    INV-MS-1  (service, method) 是分派键；未登记的组合一律按「未实现」处理
    INV-MS-2  MailService 的任何**未实现**方法都不许假成功（整服务黑名单，不是逐条列）
    INV-MS-3  FriendService.queryNotification 的应答里有 Notification；
              邮件那个同名方法绝不能拿到它（否则客户端把空表当"有新邮件"）
    INV-MS-4  登记进表的 MailService 方法必须真的能成功 —— 不许"登记了但没实现"

背景（docs/方案-邮件系统-全链路复检.md §2.3）：
    以前 ms_service 是一串 `if method == "query" / elif method == "batchGetAttribute" ...`，
    只看 method 不看 service。于是 MailService.queryNotification 命中了 FriendService 分支，
    收到 {"Notification": []}；而 avatarmailcomp.lua:532 判 `if retStatus.status and response.Notification`
    —— Lua 里空表是真值 -> hadNewMail=true -> 红点永远亮。
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ 必须先把外部副作用指到临时目录，再 import 游戏服
_TMP = tempfile.mkdtemp(prefix="oo_ms_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import config as ooconf
from game_server import ms as msmod
from game_server import player_state as pstate
from game_server.ms import registry
from game_server.transport import ms_service as svc

#: 客户端真实存在的 11 个邮件方法（avatarmailcomp.lua:388-544 逐个数的）
MAIL_METHODS = (
    "getUserMailList", "getMailContent", "setMailHaveRead", "setAllMailHaveRead",
    "setMailGiftReceived", "deleteMail", "star", "unstar",
    "syncGroupMail", "queryNotification", "readNotification",
)
#: 已经实现并登记的（M1 只读 5 个 + M2 领奖 1 个 + M3 管理 5 个 = 全部 11 个）
MAIL_IMPLEMENTED = MAIL_METHODS
UID = "ms_dispatch_u"

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


def reply(service, method, fields=None):
    raw = svc.ms_reply_params(service, method, fields or {}, 0)
    return msmod.mp_decode(raw)[0]


def req_args(*args):
    """构造 field 4 = msgpack 数组的 fields（模拟客户端请求）。"""
    return {4: [msmod.mp_arr(list(args))]}


def is_fail(resp):
    ret = resp.get("Ret") if isinstance(resp, dict) else None
    return isinstance(ret, list) and ret and ret[0] is False


def main():
    print("tmp =", _TMP)
    svc._ensure_handlers_loaded()
    strict = ooconf.unknown_fake_ok()

    # 给分派测试造一个真实玩家状态（否则 mail 处理器会回 "unknown user"）
    st = pstate.get_state({}, uid=UID)
    # 再塞一封邮件：getMailContent 对不存在的 MailId 会回失败，那是正确行为，
    # 但这里要测的是「登记了就能成功」，所以得有一封真的。
    # 不走 send_mail（那要 BDD 模板），直接给 store 一个最小合法实例，测试不依赖配表。
    from game_server.mail import store as _store
    _store.add(st.props, {
        "Title": "t", "SrcName": "s", "CreatedTime": 1, "ExpireTime": 2 ** 31,
        "Params": {"mailTemplateId": 0, "haveRead": False, "star": False,
                   "giftReceived": False},
    }, now=1)
    def make_mail():
        """塞一封最小合法邮件，返回它的 MailId。"""
        return _store.add(st.props, {
            "Title": "t", "SrcName": "s", "CreatedTime": 1, "ExpireTime": 2 ** 31,
            "Params": {"mailTemplateId": 0, "haveRead": False, "star": False,
                       "giftReceived": False},
        }, now=1)["MailId"]

    def args_for(mid):
        """每种方法用它自己能成功的最小参数。

        ★ 每一轮（严格/兼容）都要**新建邮件**：这些方法都改状态，
          共用同一封会让第二轮拿到 "mail not found"，测的就不是分派了。
        """
        return {
            "getUserMailList": req_args(UID, 0, 100),
            "getMailContent": req_args(UID, mid),
            "syncGroupMail": req_args(UID),
            "queryNotification": req_args(UID),
            "readNotification": req_args(UID),
            "setMailGiftReceived": req_args(UID, [mid]),
            "setMailHaveRead": req_args(UID, mid),
            "setAllMailHaveRead": req_args(UID),
            "deleteMail": req_args(UID, [mid]),
            "star": req_args(UID, mid),
            "unstar": req_args(UID, mid),
        }

    # ------------------------------------------------------------ INV-MS-3
    print("\n[1] INV-MS-3 好友的通知方法必须还是「成功 + Notification 字段」")
    for mode in (False, True):
        ooconf.configure_unknown_fake_ok(mode)
        f = reply("FriendService", "queryNotification")
        check(f"模式 fake_ok={mode}: 成功", not is_fail(f), f)
        check(f"模式 fake_ok={mode}: 带 Notification 字段", "Notification" in f, sorted(f))
        f2 = reply("FriendService", "fetchFriends")
        check(f"模式 fake_ok={mode}: fetchFriends 带 Friends 字段", "Friends" in f2, sorted(f2))

    # ------------------------------------------------------------ INV-MS-4
    print("\n[2] INV-MS-4 登记进表的邮件方法必须真的能成功")
    for mode in (False, True):
        ooconf.configure_unknown_fake_ok(mode)
        # 每个方法用**各自新建**的邮件，互不影响（star 之后 delete 会找不到）
        for m in MAIL_IMPLEMENTED:
            need_mail = m not in ("getUserMailList", "syncGroupMail",
                                  "queryNotification", "readNotification",
                                  "setAllMailHaveRead")
            mid = make_mail() if need_mail else 0
            r = reply("MailService", m, args_for(mid)[m])
            check(f"fake_ok={mode} MailService.{m} 成功", not is_fail(r), r)

    # ------------------------------------------------------------ INV-MS-1/2
    print("\n[3] INV-MS-1/2 未实现的邮件方法必须显式失败（两种模式都一样）")
    unimplemented = [m for m in MAIL_METHODS if m not in MAIL_IMPLEMENTED]
    check("M1~M3 已覆盖全部 11 个方法（没有遗漏的）", unimplemented == [], unimplemented)
    for mode in (False, True):
        ooconf.configure_unknown_fake_ok(mode)
        for m in unimplemented:
            r = reply("MailService", m, req_args(UID))
            check(f"fake_ok={mode} MailService.{m} 失败", is_fail(r), r)
            ret = r.get("Ret") if isinstance(r, dict) else None
            check(f"fake_ok={mode} MailService.{m} 是 not implemented",
                  isinstance(ret, list) and len(ret) > 1 and ret[1] == "not implemented", ret)
            check(f"fake_ok={mode} MailService.{m} 不带 Notification", "Notification" not in r, sorted(r))

    # ------------------------------------------------------------ INV-MS-1
    print("\n[4] INV-MS-1 分派表：键形状 + 邮件登记集合")
    ooconf.configure_unknown_fake_ok(False)
    tbl = registry.table()
    bad_key = [k for k in tbl if not (isinstance(k, tuple) and len(k) == 2)]
    check("所有键都是二元组", bad_key == [], bad_key)
    registered_mail = sorted(m for (s, m) in tbl if s == "MailService")
    check("MailService 只登记了已实现的那几个", registered_mail == sorted(MAIL_IMPLEMENTED),
          registered_mail)
    check("登记的方法都有处理器", all(callable(h) for h in tbl.values()))

    print("\n[5] INV-MS-1 同一方法名换一个服务，绝不能拿到原服务的应答")
    for key in tbl:
        service, method = key
        own = reply(service, method, args_for(make_mail())[method]
                    if method in MAIL_IMPLEMENTED else {})
        other = reply("__no_such_service__", method)
        check(f"{service}.{method} 不被别家服务认领", own != other or is_fail(other), (own, other))

    # ------------------------------------------------------------ 回归
    print("\n[6] 回归：已实现的三个基础方法行为不变")
    q = reply("GameLoadService", "query")
    check("query 成功", not is_fail(q), q)
    check("query 带 ServerList", "ServerList" in q, sorted(q))
    b = reply("UserDataService", "batchGetAttribute")
    check("batchGetAttribute 成功", not is_fail(b), b)
    check("batchGetAttribute 带 Results", "Results" in b, sorted(b))

    print("\n[7] 未登记的组合一律失败（严格模式）")
    for s, m in (("NoSuchService", "noSuchMethod"), ("MailService", "nonexistent"),
                 ("ClientGate", "bind")):
        r = reply(s, m)
        check(f"{s}.{m} 失败", is_fail(r), r)
        check(f"{s}.{m} 没带任何业务字段", set(r) <= {"Ret"}, sorted(r))

    ooconf.configure_unknown_fake_ok(strict)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
