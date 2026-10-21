# -*- coding: utf-8 -*-
"""游戏命令：/give /char /lv /money /tp /quest /story /act。

这些命令目前只做「解析 + 校验 + 说明将要发生什么」，真正生效要等游戏端
RPC 接通（见 ROADMAP 1-1 / 1-2 / 2-3 / 3-3），所以每条都带 BACKEND_HINT。
"""
from shared import gamedata

from .backend import BACKEND_HINT
from .core import _ambiguous, _need_num, PERM_GM, err, ok, register


@register("give", aliases=("给",), args="<target> <item> [num]",
          desc="给目标发放物品", perm=PERM_GM, needs_target=True, group="GM")
def cmd_give(ctx, target, *args):
    if not args:
        return err("用法：/give <目标> <物品ID或名字> [数量]",
                   chat="✗ 用法: /give <目标> <物品> [数量]")
    num, e = _need_num(args[1] if len(args) > 1 else None, 1, 1, 999999)
    if e:
        return e
    entry, cands = gamedata.resolve_item(args[0], ctx.allow_unavailables)
    if entry is None:
        if cands:
            return _ambiguous("物品", cands)
        return err("找不到物品「%s」" % args[0], chat="✗ 找不到物品")
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  物品    %s（ID %s）" % (entry["name"], entry["id"]),
        "  品质    %s    类别 %s    %s"
        % (entry.get("quality"), entry.get("type"), gamedata.tier_name(entry)),
        "  数量    %d" % num,
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines,
              chat="✔ %s ×%d → %s（未生效）" % (entry["name"], num, target),
              data={"action": "give", "target": target, "item": entry, "num": num,
                    "applied": False})


@register("char", aliases=("角色",), args="<target> <char>",
          desc="给目标解锁角色", perm=PERM_GM, needs_target=True, group="GM")
def cmd_char(ctx, target, *args):
    if not args:
        return err("用法：/char <目标> <角色ID或名字>",
                   chat="✗ 用法: /char <目标> <角色>")
    entry, cands = gamedata.resolve_char(args[0], ctx.allow_unavailables)
    if entry is None:
        if cands:
            return _ambiguous("角色", cands)
        return err("找不到角色「%s」" % args[0], chat="✗ 找不到角色")
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  角色    %s（ID %s）" % (entry["name"], entry["id"]),
        "  称号    %s" % (entry.get("title") or "-"),
        "  英文名  %s" % (entry.get("name_en") or "-"),
        "  品阶    %s（%s）" % (entry.get("grade"), gamedata.tier_name(entry)),
        "  地区    %s    CV %s" % (entry.get("area") or "-", entry.get("cv") or "-"),
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines,
              chat="✔ %s → %s（未生效）" % (entry["name"], target),
              data={"action": "char", "target": target, "char": entry, "applied": False})


@register("lv", aliases=("等级",), args="<target> <level>",
          desc="设置玩家等级", perm=PERM_GM, needs_target=True, group="GM")
def cmd_lv(ctx, target, *args):
    if not args:
        return err("用法：/lv <目标> <等级>", chat="✗ 用法: /lv <目标> <等级>")
    lv, e = _need_num(args[0], None, 1, 999, "等级")
    if e:
        return e
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  等级    %d" % lv,
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines, chat="✔ 等级 → %d（未生效）" % lv,
              data={"action": "lv", "target": target, "level": lv, "applied": False})


@register("money", aliases=("货币",), args="<target> <currency> <num>",
          desc="增加货币", perm=PERM_GM, needs_target=True, group="GM")
def cmd_money(ctx, target, *args):
    if len(args) < 2:
        return err("用法：/money <目标> <货币> <数量>\n可用货币：%s"
                   % "、".join(c["id"] for c in gamedata._currencies),
                   chat="✗ 用法: /money <目标> <货币> <数量>")
    num, e = _need_num(args[1], None, 1, 99999999)
    if e:
        return e
    entry, cands = gamedata.resolve_currency(args[0])
    if entry is None:
        return err("找不到货币「%s」，可用：%s"
                   % (args[0], "、".join(c["id"] for c in gamedata._currencies)),
                   chat="✗ 找不到货币")
    item = gamedata._item_by_id.get(entry.get("item_id")) or {}
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  货币    %s（对应物品 %s %s）"
        % (entry["id"], entry.get("item_id"), item.get("name") or ""),
        "  数量    +%d" % num,
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines,
              chat="✔ %s +%d → %s（未生效）" % (entry["id"], num, target),
              data={"action": "money", "target": target, "currency": entry,
                    "num": num, "applied": False})


@register("tp", aliases=("传送",), args="<target> <map>",
          desc="传送到地图", perm=PERM_GM, needs_target=True, group="GM")
def cmd_tp(ctx, target, *args):
    if not args:
        return err("用法：/tp <目标> <地图ID或名字>",
                   chat="✗ 用法: /tp <目标> <地图>")
    entry, cands = gamedata.resolve_map(args[0], ctx.allow_unavailables)
    if entry is None:
        if cands:
            return _ambiguous("地图", cands)
        return err("找不到地图「%s」" % args[0], chat="✗ 找不到地图")
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  地图    %s（ID %s）" % (entry.get("name") or "-", entry["id"]),
        "  场景    %s" % (entry.get("power") or "-"),
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines,
              chat="✔ 传送到 %s（未生效）" % (entry.get("name") or entry["id"]),
              data={"action": "tp", "target": target, "map": entry, "applied": False})


@register("quest", aliases=("任务",), args="<target> <taskId>",
          desc="完成任务", perm=PERM_GM, needs_target=True, group="GM")
def cmd_quest(ctx, target, *args):
    if not args:
        return err("用法：/quest <目标> <任务ID或描述关键词>",
                   chat="✗ 用法: /quest <目标> <任务>")
    entry, cands = gamedata.resolve_task(args[0])
    if entry is None:
        if cands:
            return _ambiguous("任务", cands)
        return err("找不到任务「%s」" % args[0], chat="✗ 找不到任务")
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  任务    #%s  %s" % (entry["id"], entry.get("desc") or ""),
        "  类型    %s    前置任务 %s" % (entry.get("type"), entry.get("pre")),
        "",
        "  ⚠ " + BACKEND_HINT,
    ]
    return ok(lines,
              chat="✔ 任务 #%s → %s（未生效）" % (entry["id"], target),
              data={"action": "quest", "target": target, "task": entry, "applied": False})


@register("story", aliases=("剧情",), args="<target> <story>",
          desc="播放剧情", perm=PERM_GM, needs_target=True, group="GM")
def cmd_story(ctx, target, *args):
    if not args:
        return err("用法：/story <目标> <剧情名，如 Test_01>",
                   chat="✗ 用法: /story <目标> <剧情名>")
    entry, cands = gamedata.resolve_story(args[0])
    if entry is None:
        if cands:
            return _ambiguous("剧情", cands)
        return err("找不到剧情「%s」" % args[0], chat="✗ 找不到剧情")
    lines = [
        "✔ 已解析",
        "  目标    %s" % target,
        "  剧情    %s（speechId %s）" % (entry["id"], entry.get("speech_id")),
        "",
        "  ⚠ 剧情播放走客户端 storydirector，需要 playStory 触发（ROADMAP 2-3）",
    ]
    return ok(lines,
              chat="✔ 剧情 %s → %s（未生效）" % (entry["id"], target),
              data={"action": "story", "target": target, "story": entry, "applied": False})


@register("act", aliases=("活动",), args="<id|name> [open|close]",
          desc="查看或开关活动", perm=PERM_GM, group="GM")
def cmd_act(ctx, target, *args):
    if not args:
        rows = gamedata._activities
        shown = rows[:15]
        lines = ["活动共 %d 个，前 %d 个：" % (len(rows), len(shown))]
        for a in shown:
            lines.append("  %-6s %-16s type=%-4s %s"
                         % (a["id"], a.get("name") or "-", a.get("type"),
                            "显示" if a.get("show") else "隐藏"))
        lines.append("")
        lines.append("  用法：/act <id或名字> [open|close]")
        return ok(lines, chat="活动 %d 个" % len(rows),
                  data={"action": "act_list", "count": len(rows)})

    entry, cands = gamedata.resolve_activity(args[0], ctx.allow_unavailables)
    if entry is None:
        if cands:
            return _ambiguous("活动", cands)
        return err("找不到活动「%s」" % args[0], chat="✗ 找不到活动")
    mode = (args[1].lower() if len(args) > 1 else "")
    if mode and mode not in ("open", "close", "开", "关"):
        return err("开关只能是 open / close", chat="✗ 只能是 open/close")
    lines = [
        "✔ 已解析",
        "  活动    %s（ID %s）" % (entry.get("name") or "-", entry["id"]),
        "  类型    %s    当前 %s"
        % (entry.get("type"), "显示" if entry.get("show") else "隐藏"),
        "  时段    %s ~ %s" % (entry.get("start"), entry.get("end")),
    ]
    if mode:
        lines += ["  操作    %s" % ("开启" if mode in ("open", "开") else "关闭")]
    lines += ["", "  ⚠ 活动状态是服务端下发（ActivityProp），RPC 待接入（ROADMAP 3-3）"]
    return ok(lines,
              chat="✔ 活动 %s%s（未生效）"
                   % (entry["id"], (" " + mode) if mode else ""),
              data={"action": "act", "activity": entry, "mode": mode, "applied": False})


