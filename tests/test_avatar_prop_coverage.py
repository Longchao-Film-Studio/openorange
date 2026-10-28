#!/usr/bin/env python
"""客户端读的每个 Avatar 属性，服务端都必须有来源 —— 守卫测试。

## 这个测试为什么存在（两次真实事故）

2026-10-28 实机：点左上角等级/头衔入口，面板**打开后立刻自我关闭**，
用户看到的是「点了没有任何反应」。客户端日志：

    node_daily_task_itemGroup.lua:86
      local firstDay = GlobalData.Avatar.title_daily_task_cache.is_first_day ~= 1
    attempt to index field 'title_daily_task_cache' (a nil value)
    <- refreshState <- showTaskInfo <- setData
    <- DirectorTitleV3Panel.lua:198 _initDailyTaskList <- :167 <- :35 initEx

同一个坑**在这之前已经发生过一次**：dungeonView 缺失 ->
按 M 打开地图崩 -> 表现为「UI 闪一下又回来」
（见 tools/client_contract.py 里 dungeonView 那段注释）。

两次都是「客户端读了某个属性，服务端没下发」，两次都表现为
「UI 没反应」，两次都要靠翻 Player.log 才能定位。
—— 这就是必须建守卫的理由：这类 bug 不该靠人工翻日志发现。

## 这个测试只做「判定」，不做「定义」

属性清单、豁免理由、推导规则全部在 tools/client_contract.py（唯一真相源）。
本文件只负责断言「那份契约对客户端现状成立」。这样生成器和测试不会各存一份
然后漂移 —— 以前正是两份清单，漂移的后果是「测试绿了、实机崩了」。

## 三条断言

  [2] 每个读点都有来源       没有 -> 红，并打印「哪个文件、缺什么」
  [3] 豁免清单不许腐烂       豁免了但客户端已经不读 -> 红（说明该删了）
  [4] SERVER_PROPS/SIMPLE_PROPS 必须真的产出  声明了但 initDict 没写 -> 红（生成器 bug）

新增缺失项会直接让这个测试红 —— 这是故意的：加一条豁免比修一个
「面板点了没反应」便宜得多，而且豁免必须写理由，不能糊过去。

用法: python -m tests.test_avatar_prop_coverage
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game_server import ms  # noqa: E402
from tools import client_contract as contract  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def main():
    if not os.path.isdir(contract.LUA_ROOT):
        print("跳过：找不到客户端 Lua 目录 " + contract.LUA_ROOT)
        return 0

    props = ms.AVATAR_INIT_DICT.get("__Properties__") or {}
    res = contract.classify()

    print("\n[1] 扫描客户端 Lua 的 GlobalData.Avatar.<x> 属性访问")
    check("扫到 >100 个不同属性名", len(res) > 100, len(res))
    check("avatar_init 属性数 > 200", len(props) > 200, len(props))
    # 读点里**大多数**应当已经有来源；如果这个比例掉下来，说明扫描或契约某一侧坏了。
    have = sum(1 for r in res.values() if r.ok)
    check("有来源的读点占比 >= 95%", have * 100 >= len(res) * 95,
          f"{have}/{len(res)}")

    print("\n[2] 每个属性式读取都有来源（下发 or 显式豁免）")
    gaps = contract.gaps()
    for r in gaps:
        where = r.files[0] if r.files else "?"
        print(f"       [{r.kind:<9}] {r.name:<34} {len(r.files)} 处，例如 {where}")
        print(f"                 {r.reason}")
    check("没有未解释的读点", not gaps,
          "、".join(r.name for r in gaps) if gaps else "")

    print("\n[3] 豁免清单不许腐烂（每条必须仍然被客户端读到）")
    stale = contract.stale_exempt()
    check("没有过期的豁免项", not stale, "、".join(stale) if stale else "")

    print("\n[4] 契约里声明的属性必须真的产出（否则是生成器 bug）")
    missing_types = sorted(k for k in contract.SERVER_PROPS if k not in props)
    check("SERVER_PROPS 全部已产出", not missing_types, "、".join(missing_types))
    missing_simple = sorted(k for k in contract.SIMPLE_PROPS if k not in props)
    check("SIMPLE_PROPS 全部已产出", not missing_simple, "、".join(missing_simple))

    print("\n[5] 本次实机事故的两个属性必须真的下发、且类型对")
    for name, typ in (("title_daily_task_cache", "TitleDailyTaskCache"),
                      ("street_level_info", "StreetLevelInfo")):
        node = props.get(name)
        check(f"{name} 已下发", isinstance(node, dict), type(node).__name__)
        check(f"{name} 的 __tp__ 是 {typ}",
              isinstance(node, dict) and node.get("__tp__") == typ,
              node.get("__tp__") if isinstance(node, dict) else None)

    print()
    print(f"=== 结果: PASS={len(passed)} FAIL={len(failed)} ===")
    if failed:
        print("失败项：" + "、".join(failed))
        print()
        print("怎么修（判定规则全在 tools/client_contract.py）：")
        print("  1) 缺自定义对象  -> SERVER_PROPS 加一条，重跑 python tools/build_avatar_init.py")
        print("  2) 缺标量初值    -> SIMPLE_PROPS 加一条，重跑生成器")
        print("  3) 确实不用下发  -> EXEMPT 加一条，并写明为什么")
        print("  4) 生成器没产出  -> 生成器 bug，不是契约 bug，别在契约里绕过")
        return 1
    print(f"通过 {len(passed)} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
