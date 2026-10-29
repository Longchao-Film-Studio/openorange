#!/usr/bin/env python3
"""微服务分派测试：一个方法名只能属于一个服务。

用法:
    python tests/test_ms_mail_dispatch.py

被测的不变量：

    INV-MS-1  (service, method) 是分派键；未登记的组合一律按「未实现」处理
    INV-MS-2  MailService 的任何方法都不许假成功（整服务黑名单，不是逐条列）
    INV-MS-3  FriendService.queryNotification 的真实应答里有 Notification
              -> 邮件那个同名方法绝不能拿到它（否则客户端把空表当"有新邮件"）

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

from game_server import config as ooconf  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402

#: 客户端真实存在的 11 个邮件方法（avatarmailcomp.lua:388-544 逐个数的）
MAIL_METHODS = (
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


def reply(service, method, fields=None):
    """取 (service, method) 的应答 msgpack 解码结果。"""
    raw = svc.ms_reply_params(service, method, fields or {}, 0)
    return msmod.mp_decode(raw)[0]


def is_fail(resp):
    ret = resp.get("Ret") if isinstance(resp, dict) else None
    return isinstance(ret, list) and ret and ret[0] is False


def main():
    print("tmp =", _TMP)
    strict = ooconf.unknown_fake_ok()
    print("初始模式 unknown_fake_ok =", strict)

    # ------------------------------------------------------------ INV-MS-3
    print("\n[1] INV-MS-3 好友的通知方法必须还是「成功 + Notification 字段」")
    for mode in (False, True):
        ooconf.configure_unknown_fake_ok(mode)
        f = reply("FriendService", "queryNotification")
        check(f"模式 fake_ok={mode}: 成功", not is_fail(f), f)
        check(f"模式 fake_ok={mode}: 带 Notification 字段", "Notification" in f, sorted(f))
        f2 = reply("FriendService", "fetchFriends")
        check(f"模式 fake_ok={mode}: fetchFriends 带 Friends 字段", "Friends" in f2, sorted(f2))

    # ------------------------------------------------------------ INV-MS-1 / INV-MS-2
    print("\n[2] INV-MS-1/2 邮件的方法名不许被好友分支吞掉，也不许假成功")
    for mode in (False, True):
        ooconf.configure_unknown_fake_ok(mode)
        for m in MAIL_METHODS:
            r = reply("MailService", m)
            check(f"fake_ok={mode} MailService.{m} 失败", is_fail(r), r)
            check(f"fake_ok={mode} MailService.{m} 不带 Notification", "Notification" not in r, sorted(r))

    # ------------------------------------------------------------ INV-MS-1
    print("\n[3] INV-MS-1 分派表本身：键必须是 (service, method)")
    bad_key = [k for k in svc._MS_HANDLERS if not (isinstance(k, tuple) and len(k) == 2)]
    check("所有键都是二元组", bad_key == [], bad_key)
    check("表里没有 MailService（未实现必须是显式失败）",
          not any(k[0] == "MailService" for k in svc._MS_HANDLERS), list(svc._MS_HANDLERS))

    print("\n[4] INV-MS-1 同一方法名换一个服务，绝不能拿到原服务的应答")
    ooconf.configure_unknown_fake_ok(False)
    for key in svc._MS_HANDLERS:
        service, method = key
        own = reply(service, method)
        other = reply("__no_such_service__", method)
        check(f"{service}.{method} 不被别家服务认领", own != other or is_fail(other),
              (own, other))

    # ------------------------------------------------------------ 回归
    print("\n[5] 回归：已实现的三个方法行为不变")
    ooconf.configure_unknown_fake_ok(False)
    q = reply("GameLoadService", "query")
    check("query 成功", not is_fail(q), q)
    check("query 带 ServerList", "ServerList" in q, sorted(q))
    b = reply("UserDataService", "batchGetAttribute")
    check("batchGetAttribute 成功", not is_fail(b), b)
    check("batchGetAttribute 带 Results", "Results" in b, sorted(b))

    print("\n[6] 未登记的组合一律失败（严格模式）")
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
