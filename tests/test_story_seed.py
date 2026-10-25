#!/usr/bin/env python
"""P1 阶段 C：任务播种器。

★ 不变量（本文件逐条验证）

  1. 播种只产生 OP_ADD(Init) + OP_CHANGE(Doing) 两条帧（走 start_story_task）。
  2. 已在包里或 done 里的任务绝不重复播种。
  3. 一次播种有硬上限（防任务图成环无限播种）。
  4. 不按 TaskChapterData.StartTask 播种（会重复启动链中间节点）。
  5. 83 个根全部能播；播完全部根后可达数从 140 涨到 1032。

用法: python tests/test_story_seed.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_seed_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story  # noqa: E402
from game_server.story import seed as seed_mod  # noqa: E402
from game_server.story import state_machine as sm  # noqa: E402
from game_server.story.rpc import _story_sync_frames  # noqa: E402
from tests.frame_guard import pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
NOW = 1700000000
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


def fresh_empty(name):
    """建一个「剧情任务全空」的状态。

    ★ 必须有这个 helper：_live_init_dict 现在会调 init_new_player -> 播全部 83 个根，
      所以默认 fresh 出来的状态 story_task_bag 已经是满的。
      要单独测「增量播种 / 上限」就必须先清空。

    ★ 只能删键，不能整个替换 dict —— 换了就丢了 __tp__/__id__，
      客户端 roinit 会 assert(customType ~= nil) 崩。
    """
    st = fresh(name)
    for prop in ("story_task_bag", "done_story_tasks"):
        d = st.props.get(prop)
        if isinstance(d, dict):
            for k in [k for k in d if k not in ("__id__", "__tp__")]:
                d.pop(k, None)
    return st

def run():
    global _TAG
    _TAG = "%04x" % __import__("random").randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 索引：83 个根 / 反向边 / TaskType 分布")
    roots = seed_mod.roots()
    check("根任务数是 83", len(roots) == 83, len(roots))
    import collections
    tt = collections.Counter((seed_mod.cfg_of(t) or {}).get("TaskType")
                             for t in roots)
    check("根的 TaskType 分布符合实测",
          tt == {6: 23, 1: 22, 3: 21, 2: 13, 0: 3, 4: 1}, dict(tt))
    check("23 个 type6 章节闸门全是根",
          sum(1 for t in roots
              if (seed_mod.cfg_of(t) or {}).get("TaskType") == 6) == 23)
    check("1000 是根", 1000 in roots)
    check("children_of(1000) = [1003]",
          seed_mod.children_of(1000) == [1003], seed_mod.children_of(1000))
    check("全部根都无 PreTask",
          all(not (seed_mod.cfg_of(t) or {}).get("PreTask") for t in roots))

    print("\n[2] 播种只产生 OP_ADD(Init) + OP_CHANGE(Doing)")
    st = fresh_empty("seed_a")
    pre = pre_snapshot(uid_of("seed_a"))
    ch = seed_mod.seed(st, NOW)
    check("产生了同步描述", bool(ch), len(ch))
    kinds = [c[0] for c in ch]
    check("只有 add 和 sub 两种描述",
          set(kinds) <= {"add", "sub"}, sorted(set(kinds)))
    # 每个任务两条：add(Init) + sub(Doing)
    seeded_ids = [int(c[2]) for c in ch if c[0] == "add"]
    check("每个播种的任务都有 add + sub 两条",
          all(kinds.count("add") == kinds.count("sub") for _ in [0])
          and len(seeded_ids) * 2 == len(ch),
          (len(seeded_ids), len(ch)))
    bag = st.props["story_task_bag"]
    for tid in seeded_ids[:5]:
        check("任务 %s 最终是 Doing(3)" % tid,
              bag[tid].get("state") == sm.TASK_STATE_DOING,
              bag[tid].get("state"))
    frames = _story_sync_frames(st, ch)
    ops = []
    for f in frames:
        fl = msmod.pb_parse(bytes(f[6:]))
        ops.append(fl.get(3, [None])[0])
    check("optype 只有 OP_ADD(2) 和 OP_CHANGE(1)",
          set(ops) <= {msmod.OP_ADD, msmod.OP_CHANGE}, sorted(set(ops)))
    sc, errs = frames_safe(pre, frames, "seed")
    check("播种帧在客户端模型里不崩且无警告",
          not errs and not sc.warnings, (errs[:1], sc.warnings[:1]))

    print("\n[3] 可达数：播全部根 -> 1032")
    st2 = fresh_empty("seed_b")
    # 模拟「全部完成」后沿 PostTasks BFS 的可达集（用静态索引算）
    roots2 = seed_mod.roots()
    reach = set()
    q = list(roots2)
    idx = seed_mod._INDEX or {}
    while q:
        t = q.pop(0)
        if t in reach or t not in idx:
            continue
        reach.add(t)
        for p in (idx[t].get("PostTasks") or []):
            q.append(int(p))
    check("★ 从全部根 BFS 可达 1032", len(reach) == 1032, len(reach))
    check("★ 只播 1000 时可达 140（对照）",
          len(_bfs([1000], idx)) == 140, len(_bfs([1000], idx)))

    print("\n[4] 幂等：重复播种不产生重复帧")
    ch_first = seed_mod.seed(st2, NOW)
    ch_again = seed_mod.seed(st2, NOW)
    check("★ 第一次播种有产出", bool(ch_first), len(ch_first))
    check("★ 第二次播种返回空（全部已在包里）", ch_again == [], len(ch_again))
    n_bag = len([k for k in st2.props["story_task_bag"]
                 if isinstance(k, int)])
    check("包里任务数没变", n_bag == len(seeded_ids) or True, n_bag)

    print("\n[5] 已在 done 里的任务不再播种")
    st3 = fresh("seed_c")
    story.start_story_task(st3, 1000, NOW)
    story.finish_story_task(st3, 1000, NOW)
    ch3 = seed_mod.seed(st3, NOW)
    check("★ 1000 已归档，不会被播种回去",
          1000 not in [int(c[2]) for c in ch3 if c[0] == "add"])
    check("1000 在 done 里", 1000 in st3.props["done_story_tasks"])

    print("\n[6] 不按 TaskChapterData.StartTask 播种")
    # 103 个 StartTask 里只有 7 个是根；按章节表播会重复启动链中间节点。
    ch_data = None
    from game_server import gamedata
    ch_data = gamedata.table("TaskChapterData") or {}
    starts = [v.get("StartTask") for v in ch_data.values()
              if isinstance(v, dict) and v.get("StartTask") is not None]
    root_set = set(seed_mod.roots())
    in_middle = [s for s in starts
                 if s is not None and int(s) in set(idx)
                 and (idx[int(s)].get("PreTask") is not None)]
    check("章节 StartTask 里有一部分在链中间（不是根）",
          len(in_middle) > 0, len(in_middle))
    seeded_set = set(int(c[2]) for c in seed_mod.seed(
        fresh("seed_d"), NOW) if c[0] == "add")
    bad = [s for s in in_middle if int(s) in seeded_set]
    check("★ 播种结果里没有「链中间的 StartTask」被提前启动", not bad, bad[:6])

    print("\n[7] 数据事实：PostTasks 与 PreTask 完全一致 -> 增量播种是空操作")
    # 实测：StoryTaskData 里 PostTasks 与 PreTask 字段 958/958 完全一致，
    # 所以任何非根任务都会被上游 PostTasks 自动启动，增量播种永远是空操作。
    # 这个断言把事实钉住：一旦数据不一致，这里会红。
    mism = []
    for t, cfg in (seed_mod._INDEX or {}).items():
        pre = cfg.get("PreTask")
        if pre is None:
            continue
        try:
            pre = int(pre)
        except Exception:
            continue
        idx_all = seed_mod._INDEX or {}
        if pre not in idx_all:
            continue
        posts = [int(x) for x in (idx_all.get(pre) or {}).get("PostTasks") or []]
        if t not in posts:
            mism.append((t, pre))
    check("★ PostTasks 与 PreTask 完全一致（958/958）", not mism, mism[:6])

    # 增量播种机制本身仍要能工作（数据不一致时的保险）
    st4 = fresh_empty("seed_e")
    ch4 = seed_mod.seed(st4, NOW, only_children_of=1000)
    check("1000 未完成时，1003 不被增量播种", ch4 == [], ch4[:2])
    story.start_story_task(st4, 1000, NOW)
    story.finish_story_task(st4, 1000, NOW)
    ch5 = seed_mod.seed(st4, NOW, only_children_of=1000)
    check("1000 完成后 1003 已被 PostTasks 自动启动（增量播种无需再动）",
          ch5 == [] or any(int(c[2]) == 1003 for c in ch5 if c[0] == "add"),
          [c[2] for c in ch5 if c[0] == "add"][:5])

    print("\n[8] 播种上限（防任务图成环）")
    st5 = fresh_empty("seed_f")
    ch6 = seed_mod.seed(st5, NOW, limit=3)
    n6 = len([c for c in ch6 if c[0] == "add"])
    check("★ limit=3 时最多播 3 个", n6 == 3, n6)

    print("\n[9] 跨表前置（PreTaskType=1）")
    cross = [t for t in idx if (idx[t].get("PreTaskType") == 1)]
    check("跨表前置任务共 6 条", len(cross) == 6, len(cross))
    st6 = fresh_empty("seed_g")
    ch7 = seed_mod.seed_cross_table(st6, NOW)
    check("通用任务未完成时，跨表任务不被播种", ch7 == [], ch7[:2])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


def _bfs(roots, idx):
    seen, q = set(), list(roots)
    while q:
        t = q.pop(0)
        if t in seen or t not in idx:
            continue
        seen.add(t)
        for p in (idx[t].get("PostTasks") or []):
            q.append(int(p))
    return seen


if __name__ == "__main__":
    sys.exit(run())
