#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/story_coverage.py —— 剧情任务覆盖率统计（P1 阶段 C/D 的验收工具）。

为什么必须有这个工具
--------------------
「1047 条任务里有多少条可达 / 可完成」这个数字以前是手写进文档的，
按反补丁驱动 R5（文档数字必须现场生成）不允许。这个脚本把口径固定下来：

  可达    = 从「服务端会播种的根集合」沿 PostTasks BFS 能走到的任务数
  可完成  = 可达任务里，按当前服务端实现有完成路径的数量

用法
----
    python tools/story_coverage.py            # 人类可读报告
    python tools/story_coverage.py --json     # 机器可读（给文档/CI 用）

数据来源：<repo>/../archive/game_data/BDD/TableData.CommonData.StoryTaskData.json
（可用环境变量 OO_BDD_DIR 覆盖）
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BDD_DIR = os.environ.get("OO_BDD_DIR") or os.path.join(
    ROOT, "..", "archive", "game_data", "BDD")

# 服务端已实现的 EventType（game_server/story/events.py:55-64）
IMPL_EVENTS = {
    "FinishDungeon", "FinishTask", "StreetInteract",
    "SubmitItem", "AvatarAttr", "GetItem", "CharData",
}

# 服务端会为这些 EventType 塞进 params 的键（决定 event_matches 能否命中）
# 依据：各 ev_* 构造函数实际传的 dict。
EVENT_PARAM_KEYS = {
    "FinishDungeon": {"typeId", "modeId", "level", "levelId", "isWin",
                      "first_pass", "areaid", "instructionid"},
    "FinishTask": {"taskId"},
    "StreetInteract": {"InteractId", "interactId", "actorId",
                       "actorType", "streetId"},
    "SubmitItem": set(),
    "AvatarAttr": {"level", "attr", "value", "prestigeLevel", "streetId",
                   "activity", "total_charge"},
    "GetItem": {"itemId", "count", "itemType", "level", "grade"},
    "CharData": {"level", "ascend", "charId"},
}


def _table(name):
    p = os.path.join(BDD_DIR, "TableData.CommonData.%s.json" % name)
    if not os.path.exists(p):
        print("[coverage] 找不到 %s" % p, file=sys.stderr)
        return {}
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    return d if isinstance(d, dict) else {}


def _keys_of(entry, *names):
    """取 entry 的键集合，跳过 BDD 元数据键。"""
    out = set()
    for k in names:
        v = entry.get(k)
        if isinstance(v, dict):
            out |= {x for x in v if x not in ("KeyList", "__bdd_metatype__")}
    return out


def build():
    S = {int(v["TaskID"]): v for v in _table("StoryTaskData").values()
         if isinstance(v, dict) and v.get("TaskID") is not None}
    T = {int(v["uiD"]): v for v in _table("TaskData").values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    E = {int(v["uiD"]): v for v in _table("TaskEventData").values()
         if isinstance(v, dict) and v.get("uiD") is not None}
    return S, T, E


def roots_of(S):
    return sorted(t for t, v in S.items() if not v.get("PreTask"))


def bfs(roots, S):
    seen, q = set(), list(roots)
    while q:
        t = q.pop(0)
        if t in seen or t not in S:
            continue
        seen.add(t)
        for p in (S[t].get("PostTasks") or []):
            q.append(int(p))
    return seen


def event_types_needed(S, T, E, tid):
    """这个任务靠哪些 EventType 完成（TaskType 3/7 才有）。"""
    out = []
    for pid in (S[tid].get("Parameter1") or []):
        d = T.get(int(pid))
        if not d:
            continue
        for eid in (d.get("TriggerEventList") or []):
            e = E.get(int(eid))
            if e:
                out.append(e.get("EventType"))
    return out


# 阶段 P 修正：type4 的完成条件是「切街到 Parameter1[0]」，
# 所以需要知道哪些 id 是合法街区。StreetData 是 dict（键是街区 id 字符串）。
_STREET_IDS = None


def street_ids():
    """合法街区 id 集合（StreetData 的键）。"""
    global _STREET_IDS
    if _STREET_IDS is None:
        _STREET_IDS = {int(k) for k in _table("StreetData").keys()
                       if str(k).lstrip("-").isdigit()}
    return _STREET_IDS


def completable(S, T, E, tid, SP=None):
    """按当前实现，这个任务有没有完成路径。"""
    cfg = S[tid]
    tt = cfg.get("TaskType")
    if tt in (1, 2, 5):
        return True, "客户端上报型"
    if tt == 6:
        return True, "链式（check_chain_tasks）"
    if tt == 0:
        # 阶段 J 修正（推翻阶段 E 的结论）：type0 有完成路径。
        # SpaceTaskData.avatarTask 指向剧情任务，270 条 type0 全部被指向。
        # 客户端 battlecontainer/base/player.lua:416-425 finishSpaceTask 发
        # FinishSpaceTask_Directly_CS {taskId}，服务端按 avatarTask 完成剧情任务。
        # 阶段 E 只看 StoryTaskData 就下了「无完成条件」的结论，漏了 SpaceTask 这层。
        if SP and tid in SP:
            return True, "SpaceTask 驱动（SpaceTask %s，副本 %s 关卡 %s）" % (
                SP[tid].get("id"), SP[tid].get("dungeonId"),
                SP[tid].get("levelId"))
        return False, "type0 但没有 SpaceTask.avatarTask 指向它"
    if tt == 4:
        # ★ 阶段 P 修正（推翻阶段 E 的结论）：完成信号 = 切街到 Parameter1[0]。
        #   依据 docs/storytaskresearch.md §16 / §19：
        #     - 13 条 type4 的 Parameter1 全是目标街区 id
        #     - 能验证的 4 条 transferStreet 与 Parameter1 4/4 一致
        #     - 客户端不上报 type4（getTaskIdByParamInt 只筛 type2）
        #   旧结论「StreetInteract 命中 GuideParam[0]」是错的：
        #     1045 的 GuideParam[0]=13164 在 StreetActorInteractData 里一条都没有。
        p1 = cfg.get("Parameter1") or []
        if not p1:
            return False, "type4 但没有 Parameter1（无目标街区）"
        try:
            want = int(p1[0])
        except Exception:
            return False, "type4 的 Parameter1[0] 不是整数"
        if want not in street_ids():
            return False, "type4 的 Parameter1[0]=%s 不是合法 StreetData 键" % want
        return True, "街区型（切街到 Parameter1[0]=%s）" % want
    if tt in (3, 7):
        # 阶段 I 修正：Parameter1 对 type3/7 不总是指向 TaskData。
        # 实测 92 条指向 TaskData（有 TriggerEventList，可判定），
        # 66 条指向 LevelData / EquipmentData / HonorWallData / CommodityData /
        # 2V2* / ActivityData 等玩法表 —— 那是地图导航目标
        # （avatarstorytaskcomp.lua:116 GetLevelData(Parameter1[1])），
        # 不是完成条件，没有 TriggerEventList 可匹配。
        p1 = cfg.get("Parameter1") or []
        in_taskdata = [int(x) for x in p1 if int(x) in T]
        if not in_taskdata:
            return False, "Parameter1 指向玩法表（导航目标，无完成条件）"
        ets = event_types_needed(S, T, E, tid)
        if not ets:
            return False, "Parameter1 在 TaskData 里但没有 TriggerEventList"
        ok = [e for e in ets
              if e in IMPL_EVENTS
              and _keys_of(E_by_type(E, e), "EventParam")
              <= EVENT_PARAM_KEYS.get(e, set())]
        if ok:
            return True, "事件型（%s）" % ",".join(sorted(set(ok)))
        return False, "需要未实现事件：%s" % ",".join(
            sorted(set(e for e in ets if e not in IMPL_EVENTS)))
    return False, "未知 TaskType=%s" % tt


def E_by_type(E, etype):
    for e in E.values():
        if e.get("EventType") == etype:
            return e
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--limit", type=int, default=15,
                    help="每种阻塞原因最多列几条")
    args = ap.parse_args()

    S, T, E = build()
    # SpaceTask -> 剧情任务 的反查表（阶段 J）
    SP = {}
    _sp = _table("SpaceTaskData")
    for _dg, _group in (_sp or {}).items():
        if not isinstance(_group, dict):
            continue
        for _tid, _row in _group.items():
            if not isinstance(_row, dict):
                continue
            _a = _row.get("avatarTask")
            if _a is None:
                continue
            try:
                SP.setdefault(int(_a), _row)
            except Exception:
                continue
    if not S:
        print("[coverage] StoryTaskData 读不到，退出", file=sys.stderr)
        return 2
    roots = roots_of(S)
    reach = bfs(roots, S)
    unreach = set(S) - reach

    # 可完成判定
    ok_ids, blocked = [], collections.defaultdict(list)
    for t in sorted(reach):
        good, why = completable(S, T, E, t, SP)
        if good:
            ok_ids.append(t)
        else:
            blocked[why.split("（")[0].split("：")[0]].append(t)

    by_type_reach = collections.Counter(S[t].get("TaskType") for t in reach)
    by_type_ok = collections.Counter(S[t].get("TaskType") for t in ok_ids)

    report = {
        "total": len(S),
        "roots": len(roots),
        "reachable": len(reach),
        "unreachable": len(unreach),
        "unreachable_ids": sorted(unreach)[:50],
        "completable": len(ok_ids),
        "blocked": len(reach) - len(ok_ids),
        "reachable_by_type": dict(sorted(by_type_reach.items())),
        "completable_by_type": dict(sorted(by_type_ok.items())),
        "blocked_reasons": {k: len(v) for k, v in
                            sorted(blocked.items(), key=lambda x: -len(x[1]))},
        "blocked_samples": {k: v[:args.limit] for k, v in blocked.items()},
        "coverage_pct": round(100.0 * len(ok_ids) / max(1, len(reach)), 1),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    W = 66
    print("=" * W)
    print("剧情任务覆盖率（tools/story_coverage.py，数字现场生成）")
    print("=" * W)
    print("StoryTaskData 总条数      : %d" % report["total"])
    print("无 PreTask 的根           : %d" % report["roots"])
    print("从全部根 PostTasks BFS 可达: %d（%.1f%%）"
          % (report["reachable"], 100.0 * report["reachable"] / report["total"]))
    print("  任何根都不可达           : %d  %s"
          % (report["unreachable"], report["unreachable_ids"][:8]))
    print("其中按当前实现可完成       : %d（%.1f%% of 可达）"
          % (report["completable"], report["coverage_pct"]))
    print("  阻塞                     : %d" % report["blocked"])
    print("-" * W)
    print("可达任务的 TaskType 分布   : %s" % report["reachable_by_type"])
    print("可完成任务的 TaskType 分布 : %s" % report["completable_by_type"])
    print("-" * W)
    print("阻塞原因：")
    for why, cnt in report["blocked_reasons"].items():
        print("  %-46s %d" % (why, cnt))
        print("      例: %s" % report["blocked_samples"][why][:8])
    print("=" * W)
    return 0


if __name__ == "__main__":
    sys.exit(main())
