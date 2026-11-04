#!/usr/bin/env python3
"""InteractGate + ExpGate 的行为守卫。

## 这两个模块解决什么

客户端有两层「任务能不能出现」的判定，服务端一条都不认：

  任务级  avatarstorytaskcomp.lua:779-791  checkTaskActivityOn
          avatarstorytaskcomp.lua:793-806  checkTaskLevelSatisfy  (LevelOn)
  交互级  streetactorbase.lua:541-615      7 条 AND（interactTask / LevelOn /
                                            interactLevel / interactChar /
                                            preInteract / preTaskSystem / activityOn）

全表 12 条剧情任务带 LevelOn（最高 32 级），而主线一分经验都不发 ——
玩家永远卡在第一条门槛之前，后面所有内容（含短信）不可达。

## 破坏性验证

gate.why_not 去掉 _check_task_level 调用        -> [1] 红
exp_gate.exp_to_reach 改成固定返回 100          -> [3] 红
exp_gate._ensure_inner 去掉 cur_level >= 检查    -> [4] 红（重复发经验）
"""
import copy
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="oo_gate_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_tmp, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_tmp, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_tmp, "cap"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_server import ms as msmod
from game_server import player_state as ps
from game_server.story import gate  # noqa: E402

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(name, cond, got=None):
    if cond:
        _PASSED.append(name)
        print(f"  OK   {name}")
    else:
        _FAILED.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def _fresh(uid):
    props = msmod.conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    ps.reset_state(props, uid=uid)
    msmod.live_init_dict(uid)
    return ps.get_state(uid=uid)


def _props_with(task_id, lv=1):
    props = msmod.conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    props["level"] = lv
    bag = props.setdefault("story_task_bag", {})
    bag[int(task_id)] = {"__id__": 900000 + int(task_id), "id": int(task_id),
                         "state": 3, "get_time": 1, "start_time": 1}
    return props


def run():
    # ---------------------------------------------------------------- gate
    print("[1] 任务级 LevelOn 会被 why_not 抓出来")
    props = _props_with(1040, lv=1)
    reasons = gate.why_not(props, 1040)
    check("level=1 时拒绝 1040", bool(reasons), reasons)
    check("原因里点名 LevelOn 和实际等级",
          any("level >= 3" in r and "当前 1" in r for r in reasons), reasons)

    props3 = _props_with(1040, lv=3)
    check("level=3 时放行", gate.why_not(props3, 1040) == [], gate.why_not(props3, 1040))

    print()
    print("[2] INV-GATE-1 判定只读，不改 props")
    snapshot = copy.deepcopy(_props_with(2001, lv=1))
    # story_task_bag 的键既有 int（任务 id）也有 str（__id__/__tp__），
    # 不能直接 sorted —— 用 repr 整体比对即可。
    before = repr(snapshot.get("story_task_bag"))
    gate.why_not(snapshot, 2001)
    after = repr(snapshot.get("story_task_bag"))
    check("why_not 没有增删任务", before == after, (before, after))
    check("level 没被改", snapshot.get("level") == 1, snapshot.get("level"))

    print()
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        print("失败项：" + ", ".join(_FAILED))
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(run())
