#!/usr/bin/env python
"""票据有效期与指令限流（雷6）回归测试。

背景：票据（ticket）同时用于游戏登录和游戏内指令通道 ——
客户端把 pc.ticket 一直复用（launcher/lua/oo/03_command.lua:114/82），
所以**不能**做一次性消费，否则第一条命令之后全部 401。

现在的处置：
  - 有效期 24h -> 8h（覆盖一次长时间游玩，泄露窗口缩小 3 倍）
  - /api/chat/command 按账号限流（30 次 / 60 秒）
  - 票据仍然可在会话内重复使用（这是聊天指令通道的前提）

用法: python tests/test_ticket_security.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_ticket_db_"), "data.db")
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_ticket_state_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import db, models  # noqa: E402
from web import app as webapp  # noqa: E402
from web import ratelimit  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    db.init()

    print("\n[1] 票据有效期：8 小时（不再是 24 小时）")
    check("TICKET_TTL = 8h", db.TICKET_TTL == 8 * 3600, db.TICKET_TTL)
    now = int(time.time())
    db.issue_ticket("t_default", "u_ticket", "acc_t")
    row = db.get_ticket("t_default")
    ttl = int(row["expire_ts"]) - int(row["issued_ts"])
    check("默认 TTL 落地为 8h", ttl == 8 * 3600, ttl)
    check("过期时间在未来", int(row["expire_ts"]) > now)

    print("\n[2] 票据在会话内可重复使用（聊天指令通道的前提）")
    ok1 = models.check_ticket("t_default") is not None
    ok2 = models.check_ticket("t_default") is not None
    ok3 = models.check_ticket("t_default") is not None
    check("★ 连续三次校验都通过（不是一次性消费）", ok1 and ok2 and ok3,
          (ok1, ok2, ok3))

    print("\n[3] 过期票据被拒")
    db.issue_ticket("t_expired", "u_ticket", "acc_t", ttl=-10)
    check("过期票据返回 None", models.check_ticket("t_expired") is None)
    check("不存在的票据返回 None", models.check_ticket("no_such") is None)

    print("\n[4] 指令限流：窗口内 30 次，超出拒绝")
    ratelimit.reset_rate()
    allowed = [ratelimit.rate_allow("chat:u1", limit=3, window=60, now=1000 + i)
               for i in range(5)]
    check("前 3 次放行", allowed[:3] == [True, True, True], allowed)
    check("第 4、5 次拒绝", allowed[3:] == [False, False], allowed)
    check("不同 key 互不影响",
          ratelimit.rate_allow("chat:u2", limit=3, window=60, now=1001) is True)

    print("\n[5] 窗口滚动后恢复")
    check("窗口外重新放行",
          ratelimit.rate_allow("chat:u1", limit=3, window=60, now=1000 + 61) is True)

    print("\n[6] web.app 真的接上了限流")
    check("web.app 导入了 rate_allow", hasattr(webapp, "_rate_allow"))
    check("默认参数是 30 次 / 60 秒",
          ratelimit.CHAT_MAX == 30 and ratelimit.CHAT_WINDOW == 60,
          (ratelimit.CHAT_MAX, ratelimit.CHAT_WINDOW))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
