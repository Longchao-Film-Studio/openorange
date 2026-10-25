#!/usr/bin/env python
"""roadmap 队列第 2 项：功能解锁修复（83 个系统入口）。

★ 背景（docs/功能解锁research.md / docs/检讨-功能解锁遗漏.md）
  客户端按钮可见 = 数据条件 AND system_p.unlocked_system[sysId]
  而服务端 _recalc_story_unlocks 以前**只处理 unlockType=2**：
    unlockType=1（通用任务 40 条）/ 3（等级 21 条）从来不解锁 -> 73% 入口没按钮。

★ 本测试锁住的东西
  1. 三类 unlockType 的数据条件判定和客户端 checkSystemUnlockState 一一对应
  2. 三种触发时机都能解锁（剧情任务完成 / 通用任务完成 / 等级变化 / 登录）
  3. 双向：条件不满足时摘除（防误解锁）
  4. 幂等：重复重算不重复推帧
  5. 核心断言：83 个入口逐个核对「数据条件满足 <=> 已推送」

用法: python tests/test_feature_unlock.py
"""
import os
import random
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_unlock_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    tasks,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.story import events as sev  # noqa: E402
from game_server.story import rpc as srpc  # noqa: E402
from game_server.story.tasks import drain_event_task_sync  # noqa: E402

passed, failed = [], []
_TAG = ""


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return "%s_%s" % (name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def _unlocked(st):
    d = (st.props.get("system_p") or {}).get("unlocked_system") or {}
    return sorted(k for k in d if isinstance(k, int))


def _sat_ids(st):
    out = []
    for row in gamedata.all_system_catalogue():
        if isinstance(row, dict) and srpc._unlock_data_satisfied(st, row):
            out.append(row.get("id"))
    return sorted(x for x in out if isinstance(x, int))


def _rows_by_type():
    out = {1: [], 2: [], 3: []}
    for row in gamedata.all_system_catalogue():
        if isinstance(row, dict) and row.get("unlockType") in out:
            out[row["unlockType"]].append(row)
    return out


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 数据：83 个入口按 unlockType 分布")
    by = _rows_by_type()
    total = sum(len(v) for v in by.values())
    check("★ 共 83 个入口", total == 83, total)
    check("★ unlockType=1（通用任务）40 条", len(by[1]) == 40, len(by[1]))
    check("★ unlockType=2（剧情任务）22 条", len(by[2]) == 22, len(by[2]))
    check("★ unlockType=3（等级）21 条", len(by[3]) == 21, len(by[3]))

    print("\n[2] 新号：什么都没达成 -> 一个都不该解锁")
    st1 = fresh("blank")
    check("★ 新号 unlocked_system 为空", _unlocked(st1) == [], _unlocked(st1))
    check("数据条件满足数也是 0", _sat_ids(st1) == [], _sat_ids(st1))

    print("\n[3] 剧情任务完成 -> unlockType=2 解锁")
    st3 = fresh("story")
    from game_server import story
    story.start_story_task(st3, 1005, 1700000000)
    set(_unlocked(st3))
    fr = srpc._finish_story_tasks(st3, [1005], source="client")
    after = set(_unlocked(st3))
    want2 = {r["id"] for r in by[2] if r.get("unlockParam") == 1005}
    check("★ 1005 完成后对应入口解锁", want2 <= after, (want2, after))
    check("解锁产生了同步帧", len(fr) > 0, len(fr))

    print("\n[4] 通用任务完成 -> unlockType=1 解锁（本项修复的核心）")
    st4 = fresh("generic")
    # 走真实链：Sign -> task10 -> 播种 -> 副本 -> task1
    sev.apply_event(st4, sev.ev_sign())
    tasks.seed_tasks(st4)
    check("task 1 已进包", tasks.task_state(st4.props, 1) == tasks.TASK_STATE_DOING,
          tasks.task_state(st4.props, 1))
    n_before = len(_unlocked(st4))
    sev.apply_event(st4, sev.ev_dungeon_finish(1004, 1, 1))
    check("★ task 1 完成",
          tasks.task_state(st4.props, 1) == tasks.TASK_STATE_FINISHED,
          tasks.task_state(st4.props, 1))
    after4 = _unlocked(st4)
    check("★ 完成序章后大量入口解锁（>20 个）", len(after4) - n_before > 20,
          len(after4) - n_before)
    want1 = {r["id"] for r in by[1] if r.get("unlockParam") == 1}
    check("★ unlockType=1 且 param=1 的入口全部解锁",
          want1 <= set(after4), (len(want1), len(want1 & set(after4))))

    print("\n[5] 等级变化 -> unlockType=3 解锁")
    st5 = fresh("level")
    lv_need = min(int(r["unlockParam"]) for r in by[3]
                  if isinstance(r.get("unlockParam"), int))
    check("数据里最低的等级门槛 = %s" % lv_need, lv_need > 1, lv_need)
    set(_unlocked(st5))
    st5.props["level"] = lv_need
    srpc._recalc_system_unlocks(st5)
    after5 = set(_unlocked(st5))
    want3 = {r["id"] for r in by[3]
             if isinstance(r.get("unlockParam"), int)
             and r["unlockParam"] <= lv_need}
    check("★ 等级达标后对应入口解锁", want3 <= after5, (want3, after5))
    check("★ 未达标的等级入口仍锁着",
          all(r["id"] not in after5 for r in by[3]
              if isinstance(r.get("unlockParam"), int)
              and r["unlockParam"] > lv_need))

    print("\n[6] 双向：条件不满足时摘除（防误解锁）")
    st6 = fresh("reverse")
    st6.props["level"] = 30
    srpc._recalc_system_unlocks(st6)
    n6a = len(_unlocked(st6))
    check("等级 30 时解锁了一批", n6a > 0, n6a)
    st6.props["level"] = 1
    srpc._recalc_system_unlocks(st6)
    n6b = len(_unlocked(st6))
    check("★ 等级降回 1 后那些等级入口被摘除", n6b < n6a, (n6a, n6b))

    print("\n[7] 幂等：重复重算不重复推帧")
    st7 = fresh("idem")
    st7.props["level"] = 30
    f1 = srpc._recalc_system_unlocks(st7)
    f2 = srpc._recalc_system_unlocks(st7)
    check("第一次有帧", len(f1) > 0, len(f1))
    check("★ 第二次没有帧（幂等）", f2 == [], len(f2))

    print("\n[8] 事件总线会带出解锁帧")
    st8 = fresh("bus")
    sev.apply_event(st8, sev.ev_sign())
    tasks.seed_tasks(st8)
    res8 = sev.apply_event(st8, sev.ev_dungeon_finish(1004, 1, 1))
    fr8 = drain_event_task_sync(st8, res8)
    check("★ 事件结果里带了解锁帧", len(fr8) > 0, len(fr8))

    print("\n[9] ★ 核心断言：83 个入口逐个核对（数据条件 <=> 已推送）")
    for stx, tag in ((st1, "新号"), (st4, "完成序章"), (st6, "等级降回1")):
        sat = set(_sat_ids(stx))
        got = set(_unlocked(stx))
        check("★ [%s] 漏解锁为 0" % tag, not (sat - got), sorted(sat - got)[:5])
        check("★ [%s] 误解锁为 0" % tag, not (got - sat), sorted(got - sat)[:5])

    print("\n[10] tools/feature_coverage.py 能跑")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run([sys.executable, os.path.join(root, "tools",
                            "feature_coverage.py"), "--json"],
                           capture_output=True, timeout=120)
        ok = p.returncode in (0, 1) and b"visible" in p.stdout
        check("★ 工具能跑且输出含 visible", ok, p.returncode)
    except Exception as e:
        check("★ 工具能跑且输出含 visible", False, e)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())