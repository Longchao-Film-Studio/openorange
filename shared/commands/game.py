"""游戏命令：/give /char /lv /money /tp /quest /story /act。

★ 诚实化（2026-10-23）：不再「解析+预览+未生效」然后假装成功了。
  - /give /lv /money  真接线到游戏服管理接口（admin_api /player/grant、/player/level），
    成功才算成功，失败明确报「连不上游戏服」。
  - /char /tp /quest /story /act  后端没有对应入口，标 implemented=False，
    控制台显示「[尚未实现]」，不再谎报「✔ …（未生效）」。
  （char 解锁需要 characterBag 结构 + 发帧，属阶段 2/3 活，见 ROADMAP）
"""
from shared import gamedata

from .core import PERM_GM, _ambiguous, _need_num, err, ok, register


def _admin_down_result(e):
    """连不上游戏服时的诚实失败（不能谎报成功）。"""
    return err("连不上游戏服：%s" % e,
               chat="✗ 游戏服未运行或管理接口不可达")


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
    # ★ 真写入：调游戏服 /player/grant
    res, e = target.call("POST", "/player/grant",
                         {"items": {str(entry["id"]): num}})
    if e:
        return _admin_down_result(e)
    if not res or not res.get("ok"):
        return err(res.get("error") if res else "grant 失败", chat="✗ 发放失败")
    granted = (res.get("result") or {}).get("granted") or []
    now = None
    for g in granted:
        if g.get("item_id") == entry["id"]:
            now = g.get("now")
    lines = [
        "✔ 已发放",
        "  目标    %s" % target,
        "  物品    %s（ID %s）" % (entry["name"], entry["id"]),
        "  数量    +%d" % num,
    ]
    if now is not None:
        lines.append("  现有    %d" % now)
    return ok(lines,
              chat="✔ %s ×%d → %s" % (entry["name"], num, target),
              data={"action": "give", "target": target, "item": entry,
                    "num": num, "applied": True})


@register("char", aliases=("角色",), args="<target> <char>",
          desc="给目标解锁角色", perm=PERM_GM, needs_target=True, group="GM",
          implemented=False)
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
        "✗ 未实现",
        "  角色解锁需要写入 characterBag 并下发同步帧（阶段 2/3，见 ROADMAP）。",
        "  已解析：%s（ID %s）" % (entry["name"], entry["id"]),
    ]
    return ok(lines,
              chat="✗ /char 未实现（characterBag 解锁属阶段 2/3）",
              data={"action": "char", "target": target, "char": entry,
                    "applied": False, "implemented": False})


@register("lv", aliases=("等级",), args="<target> <level>",
          desc="设置玩家等级", perm=PERM_GM, needs_target=True, group="GM")
def cmd_lv(ctx, target, *args):
    if not args:
        return err("用法：/lv <目标> <等级>", chat="✗ 用法: /lv <目标> <等级>")
    lv, e = _need_num(args[0], None, 1, 999, "等级")
    if e:
        return e
    # ★ 真写入：调游戏服 /player/level
    res, e = target.call("POST", "/player/level", {"level": lv})
    if e:
        return _admin_down_result(e)
    if not res or not res.get("ok"):
        return err(res.get("error") if res else "level 失败", chat="✗ 设置失败")
    return ok(["✔ 已设置等级",
               "  目标    %s" % target,
               "  等级    %d" % lv],
              chat="✔ %s 等级 → %d" % (target, lv),
              data={"action": "lv", "target": target, "level": lv,
                    "applied": True})


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
    if not entry.get("item_id"):
        return err("货币「%s」没有对应物品 id，无法发放" % entry["id"],
                   chat="✗ 货币 %s 无物品 id" % entry["id"])
    item = gamedata._item_by_id.get(entry.get("item_id")) or {}
    # ★ 真写入：货币本质就是物品，走 /player/grant
    res, e = target.call("POST", "/player/grant",
                         {"items": {str(entry["item_id"]): num}})
    if e:
        return _admin_down_result(e)
    if not res or not res.get("ok"):
        return err(res.get("error") if res else "grant 失败", chat="✗ 发放失败")
    return ok(["✔ 已发放",
               "  目标    %s" % target,
               "  货币    %s（物品 %s %s）"
               % (entry["id"], entry["item_id"], item.get("name") or ""),
               "  数量    +%d" % num],
              chat="✔ %s +%d → %s" % (entry["id"], num, target),
              data={"action": "money", "target": target, "currency": entry,
                    "num": num, "applied": True})


@register("tp", aliases=("传送",), args="<target> <map>",
          desc="传送到地图", perm=PERM_GM, needs_target=True, group="GM",
          implemented=False)
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
        "✗ 未实现",
        "  传送需要服务端主动下发切场景（space RPC），未接入。",
        "  已解析：%s（ID %s）" % (entry.get("name") or "-", entry["id"]),
    ]
    return ok(lines,
              chat="✗ /tp 未实现",
              data={"action": "tp", "target": target, "map": entry,
                    "applied": False, "implemented": False})


@register("quest", aliases=("任务",), args="<target> <taskId>",
          desc="完成任务", perm=PERM_GM, needs_target=True, group="GM",
          implemented=False)
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
        "✗ 未实现",
        "  强制完成任务需要走剧情/任务唯一入口（_finish_story_tasks），" +
        "未做命令直通。",
        "  已解析：#%s  %s" % (entry["id"], entry.get("desc") or ""),
    ]
    return ok(lines,
              chat="✗ /quest 未实现",
              data={"action": "quest", "target": target, "task": entry,
                    "applied": False, "implemented": False})


@register("story", aliases=("剧情",), args="<target> <story>",
          desc="播放剧情", perm=PERM_GM, needs_target=True, group="GM",
          implemented=False)
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
        "✗ 未实现",
        "  剧情播放走客户端 storydirector（playStory 触发），服务端无法单方触发。",
        "  已解析：%s（speechId %s）" % (entry["id"], entry.get("speech_id")),
    ]
    return ok(lines,
              chat="✗ /story 未实现（需客户端 storydirector 配合）",
              data={"action": "story", "target": target, "story": entry,
                    "applied": False, "implemented": False})


@register("act", aliases=("活动",), args="<id|name> [open|close]",
          desc="查看活动（开关未实现）", perm=PERM_GM, group="GM",
          implemented=False)
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
        "✗ 活动开关未实现（ActivityProp 需服务端下发 + RPC 接入，见 ROADMAP 3-3）",
        "  活动    %s（ID %s）" % (entry.get("name") or "-", entry["id"]),
    ]
    return ok(lines,
              chat="✗ /act 开关未实现",
              data={"action": "act", "activity": entry, "mode": mode,
                    "applied": False, "implemented": False})


