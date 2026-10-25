#!/usr/bin/env python
"""命令系统 <-> 游戏服管理接口 的联动测试。

架构（2026-10-21 重构后）：
  网页命令  --HTTP-->  游戏服本地管理接口（127.0.0.1:33102, X-OO-Token）
  -> 直接改游戏服**内存状态**并落盘，同步返回结果。
  不再共用 SQLite 表。

测试用独立的 OO_STATE_DIR，管理接口起在测试端口，不碰真实数据。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_cmd_")
# ★ 也要隔离账号库，否则会往真实的 shared/data.db 里读写
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_cmd_db_"), "data.db")
_TEST_PORT = 33197
_TEST_TOKEN = "testtok"
os.environ["OO_ADMIN_PORT"] = str(_TEST_PORT)
os.environ["OO_ADMIN_TOKEN"] = _TEST_TOKEN
os.environ["OO_ADMIN_URL"] = "http://127.0.0.1:%d" % _TEST_PORT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import admin_api

from tests.frame_guard import guarded_handle  # noqa: E402
from shared import commands as cmds
from game_server import ms as msmod, entity_rpc as erpc, config

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
    msmod._live_init_dict(uid)
    fs = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                     msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                    "modeId": 1, "storyID": 1}]),
                     1, uid=uid)
    for f in fs:
        if not isinstance(f, (bytes, bytearray)):
            continue
        fields = msmod.pb_parse(bytes(f[6:]))
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
    config.configure_scaffold(unlock_all=False)
    # 测试库是空的，先注册几个账号（/dungeon <账号> 会校验账号存在）
    from shared import db as _db
    _db.init()
    for u in ("cmd_a", "cmd_b", "cmd_c", "cmd_d", "cmd_e", "cmd_p", "cmd_q",
              "gm1", "aixiaoji"):
        try:
            if not _db.get_account_by_username(u):
                _db.register_account(u, "pw", "acc_" + u, "fp_" + u, "sk_" + u)
        except Exception as e:
            print("  注册账号 %s 失败: %s" % (u, e))
    srv, _th = admin_api.start(port=_TEST_PORT)
    time.sleep(0.3)

    try:
        print("\n[1] /dungeon 查看（默认不覆盖）")
        r = cmds.execute("/dungeon @", ctx(actor="cmd_a"))
        check("成功", r.ok, r.chat)
        check("force_level = 0", r.data.get("force_level") == 0, r.data)

        print("\n[2] /dungeon 50003 -> 只打千手Boss战（按账号）")
        r = cmds.execute("/dungeon @ 50003", ctx(actor="cmd_a"))
        check("命令成功", r.ok, r.chat)
        check("设置到 cmd_a", r.data.get("uid") == "cmd_a", r.data)
        lv = start_levels("cmd_a")
        check("cmd_a 只下发 [50003]", lv == [50003], lv)

        print("\n[3] ★ 按账号隔离：cmd_b 不受影响")
        lv_b = start_levels("cmd_b")
        check("cmd_b 仍下发三关", lv_b == [50001, 50002, 50003], lv_b)

        print("\n[4] /dungeon 50001 -> 换一关")
        r = cmds.execute("/dungeon @ 50001", ctx(actor="cmd_a"))
        check("命令成功", r.ok, r.chat)
        lv2 = start_levels("cmd_a")
        check("cmd_a 只下发 [50001]", lv2 == [50001], lv2)

        print("\n[5] /dungeon off -> 恢复全部关卡")
        r = cmds.execute("/dungeon @ off", ctx(actor="cmd_a"))
        check("命令成功", r.ok, r.chat)
        lv3 = start_levels("cmd_a")
        check("cmd_a 恢复三关", lv3 == [50001, 50002, 50003], lv3)

        print("\n[6] 管理员给别的账号设：/dungeon cmd_d 50002")
        r = cmds.execute("/dungeon cmd_d 50002", ctx(actor="aixiaoji"))
        check("命令成功", r.ok, r.chat)
        check("设置到 cmd_d", r.data.get("uid") == "cmd_d", r.data)
        lv_d = start_levels("cmd_d")
        check("cmd_d 只下发 [50002]", lv_d == [50002], lv_d)

        print("\n[7] 普通 GM 不能给别的账号设")
        r = cmds.execute("/dungeon cmd_e 50002",
                         ctx(perm=cmds.PERM_GM, actor="gm1"))
        check("GM 被拒", not r.ok, r.chat)

        print("\n[8] 参数校验")
        r = cmds.execute("/dungeon @ abc", ctx(actor="cmd_a"))
        check("非数字报错", not r.ok, r.chat)
        r = cmds.execute("/dungeon @ 999999", ctx(actor="cmd_a"))
        check("不存在的关卡报错", not r.ok, r.chat)
        r = cmds.execute("/dungeon @ 50003", ctx(perm=cmds.PERM_PLAYER))
        check("玩家被拒", not r.ok, r.chat)

        print("\n[6] /prologue 查看")
        r = cmds.execute("/prologue @", ctx(actor="cmd_p"))
        check("成功", r.ok, r.chat)
        p = r.data.get("player") or {}
        check("story_finished[1] 未过", p.get("story_finished_1") is False, p)

        print("\n[7] /prologue skip -> 立即生效")
        r = cmds.execute("/prologue @ skip", ctx(actor="cmd_p"))
        check("命令成功", r.ok, r.chat)
        res = r.data.get("result") or {}
        check("StoryTask 1000 = 6", res.get("story_task_1000") == 6, res)
        check("story_finished[1] = True",
              res.get("story_finished_1") is True, res)
        # ★ P1 阶段 C：init_new_player 现在播全部 83 个根，
        #   所以跳过序章后 bag 里是 83 个根任务减去已归档的，不再是只剩 1003。
        bag_now = res.get("story_task_bag") or []
        check("★ 播种后包里任务数 > 50（全部根已进包）", len(bag_now) > 50, len(bag_now))
        check("★ 1003 在包里（PostTasks 自动启动）", 1003 in bag_now, bag_now[:6])
        check("★ 1000 不在包里（已归档）", 1000 not in bag_now, bag_now[:6])
        check("★ 23 个 type6 章节闸门都在包里",
              all(g in bag_now for g in (2000, 4000, 5000, 6000, 7000, 8000)),
              [g for g in (2000, 4000, 5000, 6000, 7000, 8000)
               if g not in bag_now])

        print("\n[8] 跳过之后查询状态")
        r = cmds.execute("/prologue @", ctx(actor="cmd_p"))
        p2 = r.data.get("player") or {}
        check("已过", p2.get("story_finished_1") is True, p2)
        check("1000 已完成", p2.get("story_task_1000") == 6, p2)

        print("\n[9] 重复 skip 幂等")
        r = cmds.execute("/prologue @ skip", ctx(actor="cmd_p"))
        res2 = r.data.get("result") or {}
        check("标记为本来就完成", res2.get("already_finished") is True, res2)

        print("\n[10] 查询不会创建存档（关键：否则会毁掉新号初始化）")
        r = cmds.execute("/prologue @", ctx(actor="cmd_q"))
        pq = r.data.get("player") or {}
        check("cmd_q 还没有存档", pq.get("exists") is False, pq)
        check("cmd_q 未过序章", pq.get("story_finished_1") is False, pq)
        # 查询之后存档仍不应存在
        from game_server import player_state as _ps
        check("查询后依然没有存档文件", _ps.peek("cmd_q") is None)
        # 真登录一次才会有任务 1000
        msmod._live_init_dict("cmd_q")
        st_q = _ps.peek("cmd_q")
        bag_q = (st_q.props.get("story_task_bag") or {})
        check("登录后才有任务 1000", isinstance(bag_q.get(1000), dict),
              list(bag_q.keys())[:5])

        print("\n[12] Target 抽象")
        from shared.commands import resolve_target
        c_admin = cmds.Context("aixiaoji", cmds.PERM_ADMIN)
        c_gm = cmds.Context("gm1", cmds.PERM_GM)

        t_self, e = resolve_target(c_admin, "@")
        check("@ 解析成自己", t_self is not None and t_self.is_self, t_self)
        check("Target 是 str 子类", isinstance(t_self, str), type(t_self))
        check("值就是账号名", str(t_self) == "aixiaoji", str(t_self))
        check("uid 等于账号名", t_self.uid == "aixiaoji", t_self.uid)
        check("能和字符串直接比较", t_self == "aixiaoji")

        t_other, e = resolve_target(c_admin, "cmd_a")
        check("账号名解析成功", t_other is not None, e)
        check("不是自己", t_other.is_self is False, t_other)
        check("require_admin 通过（管理员）",
              t_other.require_admin() is None, t_other.require_admin())

        t_gm, e = resolve_target(c_gm, "cmd_a")
        check("GM 跨账号被 require_admin 拒",
              t_gm.require_admin() is not None, t_gm.require_admin())

        t_self_gm, e = resolve_target(c_gm, "@")
        check("GM 操作自己不需要管理员",
              t_self_gm.require_admin() is None)

        t_bad, e = resolve_target(c_admin, "no_such_account")
        check("不存在的账号返回错误", t_bad is None and e is not None, e)

        check("account 属性拿到记录",
              isinstance(t_other.account, dict), t_other.account)

        p, pe = t_other.player("dungeon")
        check("player() 能读到游戏服状态",
              pe is None and isinstance(p, dict), (p, pe))

        print("\n[11] 游戏服没在跑时给友好错误")
        old = cmds.ADMIN_URL
        cmds.ADMIN_URL = "http://127.0.0.1:1"
        r = cmds.execute("/dungeon @", ctx())
        cmds.ADMIN_URL = old
        check("返回错误而不是抛异常", not r.ok, r.chat)
        check("提示游戏服没运行", "游戏服" in (r.text or ""), r.text)

    finally:
        srv.shutdown()

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
