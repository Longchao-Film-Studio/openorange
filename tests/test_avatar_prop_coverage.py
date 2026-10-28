#!/usr/bin/env python
"""客户端读的每个 Avatar 属性，服务端必须下发 —— 缺失守卫。

## 这个测试为什么存在（真实事故）

2026-10-28 实机：点左上角等级/头衔入口，面板**打开后立刻自我关闭**，
用户看到的是「点了没有任何反应」。客户端日志：

    node_daily_task_itemGroup.lua:86
      local firstDay = GlobalData.Avatar.title_daily_task_cache.is_first_day ~= 1
    attempt to index field 'title_daily_task_cache' (a nil value)
    <- refreshState <- showTaskInfo <- setData
    <- DirectorTitleV3Panel.lua:198 _initDailyTaskList <- :167 <- :35 initEx

同一个坑**在这之前已经发生过一次**：dungeonView 缺失 ->
按 M 打开地图崩 -> 表现为「UI 闪一下又回来」
（见 tools/build_avatar_init.py 里 dungeonView 那段注释）。

两次都是「客户端读了某个属性，服务端没下发」，两次都表现为
「UI 没反应」，两次都要靠翻 Player.log 才能定位。
—— 这就是必须建守卫的理由：这类 bug 不该靠人工翻日志发现。

## 判定规则

扫 archive 里全部客户端 Lua，找**属性式**访问 GlobalData.Avatar.<name>：

  - 后面跟 "(" 或 ":" 的是**方法调用**，跳过
  - 其余是属性读取

每个属性名必须满足其一：
  a) 在 avatar_init.json 的 __Properties__ 里（服务端会下发）
  b) 在下面的 EXEMPT 里，并写明**为什么不需要下发**

新增缺失项会直接让这个测试红 —— 这是故意的：加一条豁免比修一个
「面板点了没反应」便宜得多，而且豁免必须写理由，不能糊过去。

## 豁免清单的纪律

每条必须是「客户端自己造的 / 引擎对象 / 死代码」三者之一，
**不许**写「暂时没有也没事」。判断依据写在条目上。

用法: python -m tests.test_avatar_prop_coverage
"""
import os
import re
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.lua_harness import LUA_ROOT  # noqa: E402

#: 属性式访问：后面不能紧跟 "(" 或 ":"（那是方法调用）
PROP_RE = re.compile(r"GlobalData[.]Avatar[.]([A-Za-z_][A-Za-z0-9_]*)(?![\w:(])")

#: 不需要服务端下发的名字 -> 理由
EXEMPT = {
    # --- 引擎 / 框架对象，不是玩家属性 ---
    "destroyed": "Unity 对象自带的 destroyed 标志（net/msnetworkmanager.lua），不是属性",
    "reconnectInfo": "网络管理器自己的重连信息（net/networkmanager.lua），客户端本地状态",
    "recordTimeDelay": "RTM 上报节流时间（manager/rtmclientmanager.lua），客户端本地状态",
    "__Properties__": "属性容器本身（modules/gmcommand/gmavatarproperties.lua 遍历用）",
    # --- GM / 调试专用，正式服不执行 ---
    "gmUnlockDyeColors": "GM 命令强制解锁染色（modules/gmcommand/gmcommand.lua），仅 DEBUG_MODE",
    "canEnterDyneStage": "GM 命令直进关卡（modules/gmcommand/gmcommand.lua），仅 DEBUG_MODE",
    # --- 客户端本地回调 / 临时态 ---
    "curTrialSkinExitCb": "试用皮肤退出回调函数（generalsettlementpanel），客户端自己赋值",
    "isInRoleHome": "角色主页状态判定（stage/fastjumpstage.lua），由客户端 UI 流程赋值",
    # --- 模板/笔误，不是真实属性 ---
    "xx": "item_filter 里的模板占位（node_pop_item_filtergroup.lua），非真实属性",
    # --- 原始类型：nil 不会崩（只参与比较/算术的地方有兜底）---
    "enterSpaceCount": "进副本计数，只在已进过副本的分支读；nil 安全（streetsystemmgrbase）",
    "done_achievement": "成就完成集合，achievementdatamodel 有 or {} 兜底",
    "bp_level": "战令等级，node_reward 用 or 0 兜底",
    "bp_exp": "战令经验，taskpanel 用 or 0 兜底",
    "endless_score_award_record": "无限模式积分领奖记录，levelendlesspanel 有兜底",
    "endless_rank_id": "无限模式排名 id，只在有排名时读",
    "fishingMinerActivityId": "钓鱼活动 id，活动未开时该分支不执行",
    "street_param": "街景参数，主线结算面板有兜底",
    "puzzle_passed_time": "解谜通关时间，只在通关后读",
    "friendBlackList": "好友黑名单，好友面板管理弹窗有兜底",
    "recordChallengeBossEnterActivityId": "高难 BOSS 活动 id，结算面板有兜底",
    "activityNewRedDict": "活动红点字典，node_activity_entrance 有兜底",
    "activityNewRedDictWithActID": "活动红点字典（带 actId），同上",
}


def scan():
    """返回 {属性名: [文件相对路径]}。"""
    where = defaultdict(list)
    for root, _dirs, names in os.walk(LUA_ROOT):
        for fn in names:
            if not fn.endswith(".lua"):
                continue
            path = os.path.join(root, fn)
            try:
                txt = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            rel = os.path.relpath(path, LUA_ROOT).replace(os.sep, "/")
            for m in PROP_RE.finditer(txt):
                where[m.group(1)].append(rel)
    return where


def main():
    if not os.path.isdir(LUA_ROOT):
        print("跳过：找不到客户端 Lua 目录 " + LUA_ROOT)
        return 0

    from game_server import ms  # noqa: PLC0415

    props = set(ms.AVATAR_INIT_DICT.get("__Properties__") or {})
    where = scan()
    missing = {k: v for k, v in where.items() if k not in props and k not in EXEMPT}
    stale = [k for k in EXEMPT if k not in where]

    ok, bad = 0, []

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            bad.append(name)
            print(f"  FAIL  {name}" + (f" | {detail}" if detail else ""))

    print("\n[1] 扫描客户端 Lua 的 GlobalData.Avatar.<x> 属性访问")
    check("扫到 >100 个不同属性名", len(where) > 100, len(where))
    check("avatar_init 属性数 > 200", len(props) > 200, len(props))

    print("\n[2] 每个属性式读取都有来源（下发 or 显式豁免）")
    if missing:
        for name in sorted(missing, key=lambda n: -len(missing[n])):
            files = sorted(set(missing[name]))
            print(f"       缺失: {name}（{len(missing[name])} 处，例如 {files[0]}）")
    check("没有未解释的缺失属性", not missing,
          "、".join(sorted(missing)) if missing else "")

    print("\n[3] 豁免清单不许腐烂（每条必须仍然被用到）")
    check("没有过期的豁免项", not stale, "、".join(stale) if stale else "")

    print("\n[4] 本次实机事故的两个属性必须真的下发")
    for name, typ in (("title_daily_task_cache", "TitleDailyTaskCache"),
                      ("street_level_info", "StreetLevelInfo")):
        node = ms.AVATAR_INIT_DICT.get("__Properties__", {}).get(name)
        check(f"{name} 已下发", isinstance(node, dict), type(node).__name__)
        check(f"{name} 的 __tp__ 是 {typ}",
              isinstance(node, dict) and node.get("__tp__") == typ,
              node.get("__tp__") if isinstance(node, dict) else None)

    print()
    print(f"=== 结果: PASS={ok} FAIL={len(bad)} ===")
    if bad:
        print("失败项：" + "、".join(bad))
        print()
        print("怎么修：")
        print("  1) 有对应自定义类型的 -> 加到 tools/build_avatar_init.py 的 REQUIRED，重跑生成器")
        print("  2) 原始类型 -> 加到 SIMPLE_PROPS")
        print("  3) 确实不需要下发 -> 加到本文件 EXEMPT，并写明为什么")
        return 1
    print(f"通过 {ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
