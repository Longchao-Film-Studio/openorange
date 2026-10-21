#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令系统 → 游戏服 的联动测试（/dungeon、/prologue）。

架构：网页端（8399）和游戏服（33100）是两个进程，靠 shared/db 的
settings 表通信：
  网页命令 db.set_setting(...)  ->  游戏服每次需要时 db.get_setting(...)

测试用独立的 OO_STATE_DIR + OO_DB_PATH，不碰真实数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_cmd_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_cmd_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import db, commands as cmds
from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,
                         config)

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def ctx(perm=cmds.PERM_ADMIN, actor="cmd_a"):
    return cmds.Context(actor, perm)


def start_levels(uid):
    """进副本，返回服务端下发的 levelIds。"""
    msmod._live_init_dict(uid)
    fs = erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                     msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                    "modeId": 1, "storyID": 1}]),
                     1, uid=uid)
    for f in fs:
        if not isinstance(f, (bytes, bytearray)):
            continue
        body = bytes(f[6:])
        fields = msmod.pb_parse(body)
        params = fields.get(4, [b""])[0]
        if not params:
            continue
        try:
            decoded, _u = msmod.mp_decode(params)
        except Exception:
            continue
        if not (isinstance(decoded, list) and len(decoded) == 2):
            continue
        sp = (decoded[1] or {}).get("spaceInfo") or {}
        inner = sp.get("spaceInfo") or {}
        if "levelIds" in inner:
            return inner["levelIds"]
    return None


def run():
    db.init()
    config.configure_scaffold(unlock_all=False)
    config.configure_dungeon_last_only(False)

    print("\n[1] /dungeon 查看")
    r = cmds.execute("/dungeon", ctx())
    check("不报错", r.ok, r.chat)
    check("默认关闭", r.data.get("last_only") is False, r.data)

    print("\n[2] /dungeon last-only on -> 游戏服立即生效")
    r = cmds.execute("/dungeon last-only on", ctx())
    check("命令成功", r.ok, r.chat)
    check("库里有标记", db.get_setting_bool("dungeon_last_only") is True)
    check("config.dungeon_last_only() = True",
          config.dungeon_last_only() is True)
    lv = start_levels("cmd_a")
    check("副本只下发 [50003]（千手Boss战）", lv == [50003], lv)

    print("\n[3] /dungeon last-only off -> 恢复三关")
    r = cmds.execute("/dungeon last-only off", ctx())
    check("命令成功", r.ok, r.chat)
    check("config.dungeon_last_only() = False",
          config.dungeon_last_only() is False)
    lv2 = start_levels("cmd_b")
    check("副本下发三关", lv2 == [50001, 50002, 50003], lv2)

    print("\n[4] /dungeon 参数校验")
    r = cmds.execute("/dungeon last-only xx", ctx())
    check("非法开关报错", not r.ok, r.chat)
    r = cmds.execute("/dungeon nope on", ctx())
    check("非法子命令报错", not r.ok, r.chat)

    print("\n[5] 权限：普通玩家不能用")
    r = cmds.execute("/dungeon", ctx(perm=cmds.PERM_PLAYER))
    check("玩家被拒", not r.ok, r.chat)

    print("\n[6] /prologue 查看 + 跳过")
    r = cmds.execute("/prologue", ctx(actor="cmd_c"))
    check("不报错", r.ok, r.chat)
    check("默认未跳过", r.data.get("pending") is False, r.data)

    r = cmds.execute("/prologue skip", ctx(actor="cmd_c"))
    check("标记成功", r.ok, r.chat)
    check("库里有标记",
          db.get_setting_bool("skip_prologue:cmd_c") is True)

    print("\n[7] 下次登录应用跳过序章")
    d = msmod._live_init_dict("cmd_c")["__Properties__"]
    sf = d.get("story_finished") or {}
    check("story_finished 有整数键 1",
          any(k == 1 for k in sf if isinstance(k, int)), list(sf.keys())[:5])
    bag = d.get("story_task_bag") or {}
    check("1000 已从 story_task_bag 移除", 1000 not in bag)
    check("1003 已设为 Doing",
          isinstance(bag.get(1003), dict) and bag[1003].get("state") == 3,
          bag.get(1003))
    check("1000 进了 done_story_tasks",
          isinstance((d.get("done_story_tasks") or {}).get(1000), dict))
    check("一次性标记已清",
          db.get_setting("skip_prologue:cmd_c") is None)

    print("\n[8] 再登录不会重复应用（不应报错）")
    d2 = msmod._live_init_dict("cmd_c")["__Properties__"]
    check("story_finished 仍为真",
          any(k == 1 for k in (d2.get("story_finished") or {})
              if isinstance(k, int)))

    print("\n[9] 没标记的账号不受影响")
    d3 = msmod._live_init_dict("cmd_d")["__Properties__"]
    check("cmd_d 的 story_finished 为空",
          not [k for k in (d3.get("story_finished") or {})
               if isinstance(k, int)],
          list((d3.get("story_finished") or {}).keys())[:5])
    check("cmd_d 有任务 1000 进行中",
          1000 in (d3.get("story_task_bag") or {}))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
