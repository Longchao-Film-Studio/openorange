#!/usr/bin/env python
"""存档落盘：写前备份上一版（防止写坏了没法回滚）。

2026-10-26 的教训：短信数据被写坏（嵌套对象缺 __id__），客户端每次登录都崩，
整号登不上；救回来靠的是手动拷的一份 .bak —— 那是运气，不是机制。
现在 save() 覆盖前自动留 .bak。

用法: python tests/test_player_state_backup.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_bak_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import player_state as pstate  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    uid = "bak_user"
    print("\n[1] 首次保存：只有主文件，没有 .bak")
    # ★ load() 有保护：属性少于 10 个视为「写坏了」回退初始值，所以造 12 个属性
    props = {"npc_chat_prop": {"__id__": 1}}
    for i in range(11):
        props[f"dummy_{i}"] = {"__id__": 100 + i}
    st = pstate.PlayerState(uid=uid, props=props)
    main_p = st.path()
    bak_p = main_p + ".bak"
    check("保存成功", st.save() is True)
    check("主文件存在", os.path.exists(main_p))
    check("首次保存不产生 .bak", not os.path.exists(bak_p))

    print("\n[2] 再保存一次：上一版进 .bak")
    st.props["npc_chat_prop"]["__id__"] = 2
    check("第二次保存成功", st.save() is True)
    check(".bak 出现", os.path.exists(bak_p))
    with open(bak_p, encoding="utf-8") as f:
        old = json.load(f)
    with open(main_p, encoding="utf-8") as f:
        new = json.load(f)
    check(".bak 是上一版（__id__=1）",
          old["props"]["npc_chat_prop"]["__id__"] == 1, old["props"]["npc_chat_prop"])
    check("主文件是当前版（__id__=2）",
          new["props"]["npc_chat_prop"]["__id__"] == 2, new["props"]["npc_chat_prop"])

    print("\n[3] 只保留一份 .bak（多次保存不会堆积）")
    for v in (3, 4, 5):
        st.props["npc_chat_prop"]["__id__"] = v
        st.save()
    with open(bak_p, encoding="utf-8") as f:
        bak = json.load(f)
    check(".bak 是紧邻的上一版（__id__=4）",
          bak["props"]["npc_chat_prop"]["__id__"] == 4, bak["props"]["npc_chat_prop"])
    baks = [f for f in os.listdir(os.path.dirname(main_p)) if f.endswith(".bak")]
    check("目录里只有 1 个 .bak", len(baks) == 1, baks)

    print("\n[4] .bak 不会干扰读档")
    pstate.STATES.clear()
    st2 = pstate.get_state(uid=uid)
    check("读档读到的是主文件最新值（__id__=5）",
          st2.props["npc_chat_prop"]["__id__"] == 5, st2.props["npc_chat_prop"])

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
