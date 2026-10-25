#!/usr/bin/env python
"""P1 阶段 N-d：跨天重置 + 头衔日常刷新限额。

★ 为什么需要
  `DailyCycle(3)` 和 `TitleDailyRandom(9)` 语义上是「每天一次」。
  不重置的话做完一次就永远完成，第二天也不会再出现。
  另外 N-c 的刷新 RPC 没有限额，理论上可以反复刷奖励 —— 本阶段一并收口。

用法: python tests/test_task_daily_reset.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_daily_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import (
    rpc_index,  # noqa: E402
    tasks,  # noqa: E402
)
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []
_TAG = ""
DAY = 86400


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return f"{name}_{_TAG}"


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def _slot_tasks(st, tt):
    return [t for t in ((st.props.get("task_bag") or {}).get(tt) or {})
            if isinstance(t, int)]


def _done_tasks(st, tt):
    return [t for t in ((st.props.get("done_tasks") or {}).get(tt) or {})
            if isinstance(t, int)]


def run():
    global _TAG
    _TAG = f"{random.randint(0, 0xFFFF):04x}"
    del passed[:]
    del failed[:]

    print("\n[1] 登录会记下「今天」（幂等）")
    st1 = fresh("day")
    d1 = st1.props.get(tasks.DAILY_RESET_KEY)
    check("★ 登录后记下了天序号", isinstance(d1, int), d1)
    rt2, ch2 = tasks.daily_reset(st1)
    check("★ 同一天再调不重置", rt2 == [] and ch2 == [], (rt2, len(ch2)))

    print("\n[2] 跨天后重置每日类型并重播")
    st2 = fresh("nextday")
    # 把每日任务都做完，模拟「今天已经做过」
    daily = _slot_tasks(st2, 3) + _slot_tasks(st2, 9)
    check("有每日类任务在包里", len(daily) > 0, daily)
    for t in list(daily):
        tasks.finish_task(st2, t)
    check("全部完成（包内清空）",
          _slot_tasks(st2, 3) == [] and _slot_tasks(st2, 9) == [],
          (_slot_tasks(st2, 3), _slot_tasks(st2, 9)))
    # 把「今天」往回拨一天，触发跨天
    st2.props[tasks.DAILY_RESET_KEY] = tasks._day_of(1700000000) - 1
    rt3, _ch3 = tasks.daily_reset(st2, 1700000000)
    check("★ 跨天触发了重置", rt3 == [3, 9], rt3)
    check("★ 每日任务重新进包",
          len(_slot_tasks(st2, 3)) + len(_slot_tasks(st2, 9)) > 0,
          (_slot_tasks(st2, 3), _slot_tasks(st2, 9)))
    check("★ 完成记录被清空",
          _done_tasks(st2, 3) == [] and _done_tasks(st2, 9) == [],
          (_done_tasks(st2, 3), _done_tasks(st2, 9)))

    print("\n[3] 头衔日常刷新限额（每天 1 次）")
    st4 = fresh("limit")
    check("★ 初始可刷新", tasks.can_refresh_daily(st4) is True)
    tasks.mark_daily_refreshed(st4)
    check("★ 记一次后不可刷新", tasks.can_refresh_daily(st4) is False)
    # 跨天后恢复
    st4.props[tasks.DAILY_REFRESH_KEY]["day"] = tasks._day_of(0)
    check("★ 跨天后恢复可刷新", tasks.can_refresh_daily(st4) is True)

    print("\n[4] 走真实 RPC：第二次刷新被拒")
    st5 = fresh("rpc")
    u5 = uid_of("rpc")
    f1 = guarded_handle(rpc_index.RPC_INDEX["RequestRefreshTitleDailyTask_CS"],
                        msmod.mp_arr([1]), 1, uid=u5)
    check("第一次刷新有帧", isinstance(f1, list) and len(f1) > 0, len(f1))
    check("第一次刷新后记了次数",
          tasks.can_refresh_daily(st5) is False)
    f2 = guarded_handle(rpc_index.RPC_INDEX["RequestRefreshTitleDailyTask_CS"],
                        msmod.mp_arr([1]), 2, uid=u5)
    check("★ 第二次刷新被拒（只回一条 ACK，不改状态）",
          isinstance(f2, list) and len(f2) == 1, len(f2))

    print("\n[5] 重置不影响非每日类型")
    st6 = fresh("keep")
    n1_before = len(_slot_tasks(st6, 1))
    st6.props[tasks.DAILY_RESET_KEY] = tasks._day_of(0)
    tasks.daily_reset(st6, 1700000000)
    check("★ Beginner(1) 的任务数量不受重置影响",
          len(_slot_tasks(st6, 1)) == n1_before,
          (n1_before, len(_slot_tasks(st6, 1))))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())