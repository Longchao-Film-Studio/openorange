#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1 阶段 I 追加：Parameter1 归属分类的正确性。

★ 背景（本轮实测纠正的一个模型错误）

  以前假定 StoryTaskData.Parameter1 对 TaskType 3/7 **总是指向 TaskData**，
  所以把 67 条「TaskData 里查不到」的任务算成数据缺陷、记为不可完成。

  实测：那 67 条里 **66 条是有效的**，Parameter1 指向别的玩法表：

      LevelData                   5 条（如 8021 <- 5017）
      HonorWallData               5 条（如 8012 <- 5008）
      EquipmentData               8 条（如 8017 <- 5013）
      ActorStateTransitionData    6 条（如 7011 <- 7）
      CommodityData               8 条
      2V2EuipFor2V2Data          10 条
      2V2DefaultEquipData         9 条
      2V2MonsterMessageData       4 条
      ActivityData                4 条
      CommonStageBuffData         3 条
      2V2BOSSLevelRegisterData    2 条
      BuffresultData              1 条
      ActorStateTransitionDefineData 1 条
      任何表都没有                 1 条（413701 <- 3000006）

  依据：avatarstorytaskcomp.lua:116
        local levelData = MapUIHelper:GetLevelData(selectTaskData.Parameter1[1])
    —— Parameter1[1] 是**地图导航目标**（哪个关卡/哪件装备），不是完成条件。

★ 结论

  这 66 条是「走到某关卡/某装备」类目标，没有 TriggerEventList 可匹配，
  客户端对 type 3/7 也没有完成上报调用点。所以仍然不可完成，
  但原因是「导航目标，无完成条件」，不是「数据缺陷」。

★ 这个测试钉住分类，防止又按错的口径算覆盖率。

用法: python tests/test_story_param1_kind.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_p1k_")
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
    return S, T


def table_key_set(name):
    """一张 BDD 表的全部可用键（外层键 + uiD/id/ID）。"""
    import glob
    BDD = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..", "archive", "game_data", "BDD")
    p = os.path.join(BDD, "TableData.CommonData.%s.json" % name)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    ks = set()
    if isinstance(d, dict):
        for k, v in d.items():
            ks.add(str(k))
            if isinstance(v, dict):
                for kk in ("uiD", "id", "ID"):
                    if kk in v:
                        ks.add(str(v[kk]))
    return ks


def run():
    S, T = tables()

    print("\n[1] Parameter1 对 type3/7 的归属分布")
    in_td, other, nowhere = [], [], []
    for t, v in S.items():
        if v.get("TaskType") not in (3, 7):
            continue
        p1 = v.get("Parameter1") or []
        if not p1:
            continue
        if any(int(x) in T for x in p1):
            in_td.append(t)
        else:
            # 逐个查它在不在别的表里
            hit = None
            for x in p1:
                for nm in ("LevelData", "EquipmentData", "HonorWallData",
                           "CommodityData", "ActorStateTransitionData",
                           "2V2EuipFor2V2Data", "2V2DefaultEquipData",
                           "2V2MonsterMessageData", "ActivityData",
                           "CommonStageBuffData", "2V2BOSSLevelRegisterData",
                           "BuffresultData", "ActorStateTransitionDefineData"):
                    if str(x) in table_key_set(nm):
                        hit = nm
                        break
                if hit:
                    break
            (other if hit else nowhere).append(t)
    check("★ 指向 TaskData 的 92 条", len(in_td) == 92, len(in_td))
    check("★ 指向别的玩法表的 66 条", len(other) == 66, len(other))
    check("★ 任何表都查不到的 1 条", len(nowhere) == 1, len(nowhere))

    print("\n[2] 这些任务确实是链中间节点（纯叙事推进）")
    check("★ 67 条里 64 条有 PreTask",
          sum(1 for t in other + nowhere if S[t].get("PreTask")) == 64,
          sum(1 for t in other + nowhere if S[t].get("PreTask")))
    check("★ 67 条全部有 TaskBgStory（叙事文本）",
          all(S[t].get("TaskBgStory") for t in other + nowhere))
    check("★ GuideParam 极少（2/67）",
          sum(1 for t in other + nowhere if S[t].get("GuideParam")) == 2)

    print("\n[3] 客户端依据：Parameter1[1] 是地图导航目标")
    no_scene = sorted(t for t in other + nowhere if not S[t].get("sceneId"))
    check("★ 没有 sceneId 的只有 4 条（都是无章节的特例）",
          len(no_scene) == 4, no_scene)
    check("★ 其余 63 条都有 sceneId（街区场景）",
          sum(1 for t in other + nowhere if S[t].get("sceneId")) == 63)

    print("\n[4] 覆盖率工具按新口径分类")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sc", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "story_coverage.py"))
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    E = {int(k): v for k, v in (gamedata.table("TaskEventData") or {}).items()
         if isinstance(v, dict) and v.get("uiD") is not None}
    ok, why = sc.completable(S, T, E, other[0])
    check("★ 这些任务的阻塞原因是「导航目标」不是「数据缺陷」",
          (not ok) and "导航目标" in why, why)
    # 92 条指向 TaskData 的全部有 TriggerEventList（实测 0 条例外），
    # 所以它们的判定结果只能是「事件型可完成」或「需要未实现事件」。
    ok_cnt = sum(1 for t in in_td if sc.completable(S, T, E, t)[0])
    check("★ 92 条里有一部分按事件判定为可完成", ok_cnt > 0, ok_cnt)
    check("★ 没有一条被误判成「导航目标」",
          all("导航目标" not in sc.completable(S, T, E, t)[1]
              for t in in_td))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
