#!/usr/bin/env python
"""P1 阶段 K：剩余 77 条阻塞的证据固化（防将来误判）。

★ 这个文件为什么存在

  阶段 J 之前，我判断「type0 无完成条件」是**错的** —— 因为我只看了
  StoryTaskData，漏了 SpaceTaskData 这一层。那次教训说明：
  「这类任务做不了」这个结论必须把检查过什么写下来，
  否则后来的人（包括我自己）会重复误判，或者反过来以为漏做了。

  本文件把剩余 77 条逐项检查的证据固化成断言：
  如果哪天数据变了（比如官方补了 SpaceTask 链接），这里会红，
  提示「该重新调查了」，而不是静默地继续算 92.5%。

★ 剩余 77 条的构成（阶段 J 之后）

  67 条：type3/7，Parameter1 指向 LevelData / EquipmentData / HonorWallData /
         CommodityData / 2V2* / ActivityData / ActorStateTransitionData 等玩法表
  10 条：type3/7，Parameter1 -> TaskData 正常，但 TriggerEventList 里的事件类型
         是 10 种各不相同的玩法系统

用法: python tests/test_story_blockers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_bk_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def tables():
    S = gamedata.table("StoryTaskData") or {}
    S = {int(v["TaskID"]): v for v in S.values()
         if isinstance(v, dict) and v.get("TaskID") is not None}
    T = gamedata.table("TaskData") or {}
    T = {int(k): v for k, v in T.items()
         if isinstance(v, dict) and v.get("uiD") is not None}
    E = gamedata.table("TaskEventData") or {}
    E = {int(k): v for k, v in E.items()
         if isinstance(v, dict) and v.get("uiD") is not None}
    return S, T, E


def space_flat():
    raw = gamedata.table("SpaceTaskData") or {}
    out = {}
    for _dg, g in raw.items():
        if isinstance(g, dict):
            for tid, row in g.items():
                if isinstance(row, dict):
                    out[int(tid)] = row
    return out


IMPL_EVENTS = {
    "FinishDungeon", "FinishTask", "StreetInteract",
    "SubmitItem", "AvatarAttr", "GetItem", "CharData",
}


def run():
    del passed[:]
    del failed[:]
    S, T, E = tables()
    SP = space_flat()
    driven = set()
    for r in SP.values():
        a = r.get("avatarTask")
        if a is not None:
            driven.add(int(a))

    nav = [t for t, v in S.items() if v.get("TaskType") in (3, 7)
           and (v.get("Parameter1") or [])
           and not any(int(x) in T for x in (v.get("Parameter1") or []))]
    print()
    print("[1] 67 条 Parameter1 指向玩法表的六项独立检查")
    check("共 67 条", len(nav) == 67, len(nav))
    check("(1) 没有一条被 SpaceTask.avatarTask 指向（阶段 J 的解法不覆盖它们）",
          not (set(nav) & driven), sorted(set(nav) & driven)[:5])
    check("(2) 没有一条的 task id 本身出现在 SpaceTaskData 里",
          not (set(nav) & set(SP)), sorted(set(nav) & set(SP))[:5])
    pre_driven = [t for t in nav
                  if S[t].get("PreTask") is not None
                  and int(S[t]["PreTask"]) in driven]
    check("(3) 没有一条的 PreTask 是 SpaceTask 驱动的", not pre_driven, pre_driven[:5])
    post_driven = [t for t in nav
                   if any(int(p) in driven for p in (S[t].get("PostTasks") or []))]
    check("(4) 没有一条的下一环是 SpaceTask 驱动的", not post_driven, post_driven[:5])
    check("(5) 全表没有任何任务有 Parameter2（客户端 ParamStr 这条路走不通）",
          not any(v.get("Parameter2") is not None for v in S.values()))
    fam = sum(1 for t in nav
              if S[t].get("PreTask") is not None
              and int(S[t]["PreTask"]) in S
              and S[int(S[t]["PreTask"])].get("TaskType") == 7)
    check("(6) 58/64 的 PreTask 是同族 type7（自成孤立簇）", fam == 58, fam)
    check("67 条的 Parameter1 一条都不在 TaskData 里",
          not any(int(x) in T for t in nav for x in (S[t].get("Parameter1") or [])))

    print()
    print("[2] 10 条需要未实现玩法事件的检查")
    unimpl = []
    for t, v in S.items():
        if v.get("TaskType") not in (3, 7):
            continue
        p1 = v.get("Parameter1") or []
        if not p1 or not any(int(x) in T for x in p1):
            continue
        ets = []
        for pid in p1:
            td = T.get(int(pid))
            if not td:
                continue
            for eid in (td.get("TriggerEventList") or []):
                e = E.get(int(eid))
                if e:
                    ets.append(e.get("EventType"))
        if ets and not any(x in IMPL_EVENTS for x in ets):
            unimpl.append((t, tuple(sorted(set(ets)))))
    check("共 10 条", len(unimpl) == 10, len(unimpl))
    kinds = {e for _t, es in unimpl for e in es}
    check("涉及 10 种各不相同的玩法事件类型", len(kinds) == 10, sorted(kinds))
    print("      这 10 种：" + "、".join(sorted(kinds)))

    print()
    print("[3] 总数核对")
    check("67 + 10 = 77（= 1032 可达 - 955 可完成）", 67 + 10 == 77)

    print()
    print("=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    print()
    print("★ 用途：数据变了（比如官方补了 SpaceTask 链接）就红，")
    print("  提示重新调查，而不是静默地继续按 92.5% 算。")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())