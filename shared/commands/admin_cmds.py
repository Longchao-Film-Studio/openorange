# -*- coding: utf-8 -*-
"""管理员命令：/dungeon（副本关卡覆盖）、/prologue（跳过序章）。

这两条真正会改游戏服状态 —— 走 game_server/admin_api.py 的本地 HTTP 接口，
而不是直接改数据库（网页端和游戏服是两个进程，显式边界更好排查）。
"""
from .core import _need_num, PERM_GM, err, ok, register



# ---------------------------------------------------------------- 副本关卡

# 序章的关卡，给 /dungeon 显示用（客户端 Level_CopyData["guide_01"].rootLevel）
_GUIDE01_LEVELS = [
    (50001, "序章_中浦区天台"),
    (50002, "序章_千相会龙域"),
    (50003, "序章_千手Boss战"),
]


@register("dungeon", aliases=("副本",), args="<target> [<关卡ID> | off]",
          desc="副本关卡覆盖：只打指定关卡（例 /dungeon @ 50003 直达Boss）",
          perm=PERM_GM, needs_target=True, group="GM")
def cmd_dungeon(ctx, target, *args):
    """副本关卡覆盖 —— 按账号，语法与其它命令一致：/dungeon <target> <参数>。

    target：@ = 自己；其它为账号名（跨账号需管理员，由 execute 统一校验）

      /dungeon @                  看自己的设置
      /dungeon @ 50003            自己只打 50003
      /dungeon @ off              取消自己的覆盖
      /dungeon testplay 50003     给 testplay 设

    存法：玩家存档的 _oo_dungeon_force_level 私有字段（_oo_ 前缀会被
    ms._live_init_dict 过滤掉，客户端看不到），按账号隔离、重启不丢。

    原理：客户端 dispatchhubonline.lua:49-59 优先用服务端给的
    spaceInfo.levelIds，所以只给一个关卡 id 就只打那一关。
    适用于**所有副本**：关卡 id 属于该副本 rootLevel 才生效，否则忽略并打日志。
    """
    uid = target.uid
    p, e = target.player("dungeon")
    if e:
        return err(e, chat="✗ 连不上游戏服")
    cur = p.get("force_level") or 0

    # 无参数：显示当前状态
    if not args:
        lines = [
            "副本关卡覆盖    账号 %s" % uid,
            "  当前            %s" % ("不覆盖" if not cur else "只打 %d" % cur),
            "",
            "  用法：/dungeon <target> [<关卡ID> | off]",
            "    /dungeon @ 50003     自己只打 50003",
            "    /dungeon @ off       取消",
            "",
            "  序章 guide_01 的关卡：",
        ]
        for lid, nm in _GUIDE01_LEVELS:
            mark = "  ← 当前" if lid == cur else ""
            lines.append("    %-7s %s%s" % (lid, nm, mark))
        lines += [
            "",
            "  按账号生效，多开互不影响；重启游戏服不丢。",
            "  适用于所有副本：关卡 id 属于该副本 rootLevel 才生效。",
        ]
        return ok(lines,
                  chat="%s 副本: %s" % (uid, ("只打 %d" % cur) if cur else "不覆盖"),
                  data={"action": "dungeon_show", "uid": uid,
                        "force_level": cur, "levels": _GUIDE01_LEVELS})

    raw = (args[0] or "").strip().lower()
    if raw in ("off", "0", "关", "取消", "clear"):
        res, e = target.call("POST", "/dungeon/level", {"level": 0})
        if e or not res or not res.get("ok"):
            return err(e or ("设置失败: %s" % (res,)), chat="✗ 设置失败")
        return ok(["✔ 已取消副本关卡覆盖",
                   "  账号    %s" % uid, "",
                   "  进副本会按 Level_CopyData.rootLevel 正常下发全部关卡。"],
                  chat="✔ %s 副本: 已取消覆盖" % uid,
                  data={"action": "dungeon_set", "uid": uid, "force_level": 0})

    level, e2 = _need_num(raw, None, 1, 999999, "关卡ID")
    if e2 is not None:
        return e2

    # 关卡校验：LevelData 在 game_server.gamedata 里
    entry = None
    _bdd_ok = False
    try:
        from game_server import gamedata as _gsdata
        _bdd_ok = True
        entry = _gsdata.level(level)
    except Exception:
        _bdd_ok = False
    if _bdd_ok and entry is None:
        return err("关卡 %d 不存在（LevelData 里没有这个 id）" % level,
                   chat="✗ 关卡 %d 不存在" % level)
    name = (entry or {}).get("levelName") or "-"
    scene = (entry or {}).get("scene") or "-"
    nxt = (entry or {}).get("nextLevel") or "无"

    res, e = target.call("POST", "/dungeon/level", {"level": level})
    if e or not res or not res.get("ok"):
        return err(e or ("设置失败: %s" % (res,)), chat="✗ 设置失败")
    got = res.get("force_level") or 0
    lines = [
        "✔ 已设置副本关卡覆盖",
        "  账号    %s" % uid,
        "  关卡    %d  %s" % (level, name),
        "  场景    %s" % scene,
        "  下一关  %s" % nxt,
        "",
        "  按账号生效：只有 %s 进副本会只打这一关，其它账号不受影响。" % uid,
        "  如果该关卡不属于当前副本的 rootLevel，会被忽略并在服务端打日志。",
    ]
    return ok(lines,
              chat="✔ %s 副本: 只打 %d（%s）" % (uid, level, name),
              data={"action": "dungeon_set", "uid": uid, "force_level": got,
                    "level": entry})


# ---------------------------------------------------------------- 序章跳过


@register("prologue", aliases=("序章",), args="<target> [skip]",
          desc="序章：查看状态 / 跳过（跳过 = 直接进街区，不再打副本）",
          perm=PERM_GM, needs_target=True, group="GM")
def cmd_prologue(ctx, target, *args):
    """查看或跳过序章 —— 语法与其它命令一致：/prologue <target> [skip]。

      /prologue @              看自己的序章状态
      /prologue @ skip         跳过自己的序章（立即生效）
      /prologue testplay skip  跳过 testplay 的序章

    跳过通过游戏服管理接口**立刻**改服务端状态：
      StoryTask 1000 -> Finished、1003 -> Doing、story_finished[1] = True
    """
    uid = target.uid

    if not args:
        p, e = target.player("story")
        if e:
            return err(e, chat="✗ 连不上游戏服")
        if not p.get("exists", True):
            return ok(["序章状态    账号 %s" % uid,
                       "  该账号还没有存档（从没登录过）。",
                       "",
                       "  可以直接 /prologue %s skip 预先跳过。" % uid],
                      chat="序章 %s: 还没存档" % uid,
                      data={"action": "prologue_show", "target": uid,
                            "player": p})
        sf1 = p.get("story_finished_1")
        t1000 = p.get("story_task_1000")
        lines = [
            "序章状态    账号 %s" % uid,
            "  story_finished[1]    %s" % ("已过" if sf1 else "未过"),
            "  StoryTask 1000       %s" % (
                "已完成" if t1000 == 6 else
                "进行中" if t1000 == 3 else
                "未开始" if t1000 == -1 else "状态 %s" % t1000),
            "  进行中的剧情任务     %s" % (p.get("active") or "无"),
            "",
            "  用法：/prologue <target> [skip]",
            "    /prologue @ skip     跳过自己的序章",
        ]
        return ok(lines,
                  chat="序章 %s: %s" % (uid, "已过" if sf1 else "未过"),
                  data={"action": "prologue_show", "target": uid, "player": p})

    sub = (args[0] or "").strip().lower()
    if sub not in ("skip", "跳过"):
        return err("未知子命令「%s」，用法：/prologue <target> [skip]" % args[0],
                   chat="✗ 用法: /prologue <target> [skip]")

    res, e = target.call("POST", "/player/skip-prologue")
    if e or not res or not res.get("ok"):
        return err(e or ("跳过失败: %s" % (res,)), chat="✗ 跳过失败")
    r = res.get("result") or {}
    lines = [
        "✔ 序章已跳过（立即生效）",
        "  账号              %s" % uid,
        "  StoryTask 1000    %s" % (
            "本来就已完成" if r.get("already_finished") else "已标完成"),
        "  story_finished[1] %s" % ("True" if r.get("story_finished_1") else "False"),
        "  进行中任务        %s" % (r.get("story_task_bag") or "无"),
        "",
        "  客户端下次登录/重连就会直接进街区，不再进序章副本。",
    ]
    return ok(lines,
              chat="✔ 序章已跳过（%s）" % uid,
              data={"action": "prologue_skip", "target": uid,
                    "result": r, "applied": True})


