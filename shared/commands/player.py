"""基础命令：/help /who /search /account /server /clear。"""
import time

from shared import db, gamedata

from .core import COMMANDS, PERM_ADMIN, PERM_NAMES, PERM_PLAYER, _pad, err, lookup, ok, register


# ---------------------------------------------------------------- 命令实现
@register("help", aliases=("?", "帮助"), args="[命令]", desc="查看命令列表或某条命令的详情", group="基础")
def cmd_help(ctx, target, *args):
    if args:
        cmd = lookup(args[0])
        if cmd is None:
            return err("没有命令「{}」".format(args[0]), chat="✗ 没有命令 {}".format(args[0]))
        lines = [
            "/{} {}".format(cmd.name, cmd.args),
            "  {}".format(cmd.desc),
            "  权限: {}{}".format(PERM_NAMES.get(cmd.perm, "?"),
                            "" if cmd.implemented else "   [尚未实现]"),
        ]
        if cmd.aliases:
            lines.append("  别名: {}".format("、".join("/" + a for a in cmd.aliases)))
        return ok(lines, chat="看 {}: {}".format(cmd.name, cmd.desc))

    groups = {}
    for cmd in COMMANDS.values():
        groups.setdefault(cmd.group, []).append(cmd)

    order = ["基础", "查询", "账号", "待实现"]
    names = order + [g for g in sorted(groups) if g not in order]

    lines = []
    for gname in names:
        if gname not in groups:
            continue
        lines.append("【{}】".format(gname))
        for cmd in sorted(groups[gname], key=lambda c: c.name):
            mark = "" if cmd.implemented else "  (未实现)"
            lines.append("  /{} {}{}".format(_pad(cmd.name, 9), _pad(cmd.args, 30), cmd.desc + mark))
        lines.append("")
    lines.append("语法：/命令 <target> <参数>，target 用 @ 表示自己")
    lines.append("同名条目默认取「可用」的那条（可玩角色 / 给角色的物品）；")
    lines.append("加 --allow-unavailables 可取消该规则，改为返回全部候选。")
    chat = '共 {} 条命令，网页 /admin/command 查看完整列表'.format(len(COMMANDS))
    return ok(lines, chat=chat)


@register("who", aliases=("在线",), desc="列出在线玩家", group="基础")
def cmd_who(ctx, target, *args):
    # 只统计**真正进了游戏**的连接（game_online 表，由 game_server 维护）。
    # 以前读的是 sessions（网页会话），启动器每登录一次留一条，
    # 过期时间又长，结果 /who 把历史登录全列出来（同一个账号 8 条）。
    rows = db.list_game_online()
    if not rows:
        return ok(["当前没有游戏内在线玩家。"], chat="在线 0 人")
    lines = ['在线 {} 人：'.format(len(rows))]
    for r in rows:
        name = r.get("username") or "?"
        perm = PERM_NAMES.get(int(r.get("perm_level") or 0), "?")
        uid = r.get("uid") or "-"
        addr = r.get("addr") or "-"
        since = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("since_ts") or 0)))
        lines.append('  {:<16} {:<8} uid={:<12} {}  上线 {}'.format(name, perm, uid, addr, since))
    chat = '在线 {} 人: {}'.format(len(rows), " ".join(r.get("username") or "?" for r in rows[:4]))
    return ok(lines, chat=chat)


PAGE_SIZE = 5


@register("search", aliases=("searchid", "find", "查"), args="<关键词> [页码]",
          desc="搜索物品或角色（支持 ID / 中文名 / 英文名）", group="查询")
def cmd_search(ctx, target, *args):
    if not args:
        return err("用法：/search <关键词> [页码]", chat="✗ 用法: /search 关键词")
    keyword = args[0]
    page = 1
    if len(args) > 1:
        try:
            page = max(1, int(args[1]))
        except ValueError:
            return err("页码必须是数字", chat="✗ 页码要是数字")

    items = gamedata.search_items(keyword)
    chars = gamedata.search_chars(keyword)
    total = len(items) + len(chars)
    if total == 0:
        return err("没有匹配「{}」的物品或角色".format(keyword),
                   chat="✗ 没有匹配 {}".format(keyword))

    lines = ['搜「{}」：物品 {} 条，角色 {} 条'.format(keyword, len(items), len(chars))]

    if chars:
        lines.append("")
        lines.append('角色 ({})'.format(len(chars)))
        for ch in chars[:20]:
            lines.append('  {:<7d} {} {:<8} {}星  {}'.format(ch["id"], _pad(gamedata.tier_name(ch), 4), ch["name"], ch.get("grade") or "-", ch.get("area") or ""))

    if items:
        lines.append("")
        lines.append('物品 ({})'.format(len(items)))
        for it in items[:30]:
            lines.append('  {:<7d} {} {:<20} type={} quality={}'.format(it["id"], _pad(gamedata.tier_name(it), 4), it["name"], it.get("type"), it.get("quality")))

    # 游戏内：单行紧凑 + 分页
    flat = [("角色", c["id"], c["name"]) for c in chars] + \
           [("物品", i["id"], i["name"]) for i in items]
    start = (page - 1) * PAGE_SIZE
    chunk = flat[start:start + PAGE_SIZE]
    if not chunk:
        return ok(lines, chat='共 {} 条，没有第 {} 页'.format(total, page))
    parts = ['{}{}'.format(cid, name) for _, cid, name in chunk]
    chat = '共{}条 {}-{}: {}'.format(total, start + 1, start + len(chunk), " ".join(parts))
    if len(flat) > start + PAGE_SIZE:
        chat += '  /search {} {}'.format(keyword, page + 1)
    return ok(lines, chat=chat, data={"total": total, "page": page})


@register("account", aliases=("账号",), args="<target> [info|perm <0-2>]",
          desc="查看账号信息 / 设置权限（0=玩家 1=GM 2=管理员）",
          perm=PERM_PLAYER, needs_target=True, group="账号")
def cmd_account(ctx, target, *args):
    acc = db.get_account_by_username(target)
    if not acc:
        return err("找不到账号「{}」".format(target), chat="✗ 找不到账号 {}".format(target))

    sub = (args[0].lower() if args else "info")

    if sub == "perm":
        if ctx.perm < PERM_ADMIN:
            return err("只有管理员能改权限", chat="✗ 需要管理员权限")
        if len(args) < 2:
            return err("用法：/account {} perm <0|1|2>".format(target),
                       chat="✗ 用法: /account 名 perm 0|1|2")
        try:
            level = int(args[1])
        except ValueError:
            return err("权限等级必须是 0 / 1 / 2", chat="✗ 权限要是 0/1/2")
        if level not in PERM_NAMES:
            return err("权限等级必须是 0 / 1 / 2", chat="✗ 权限要是 0/1/2")
        db.set_perm_level(target, level)
        return ok(['已把「{}」的权限设为 {}（{}）'.format(target, level, PERM_NAMES[level])],
                  chat="✓ {} -> {}".format(target, PERM_NAMES[level]))

    perm = int(acc.get("perm_level") or 0)
    lines = [
        "账号：{}".format(acc["username"]),
        "账号 ID：{}".format(acc["account_id"]),
        '权限：{}（{}）'.format(perm, PERM_NAMES.get(perm, "?")),
        "注册：{}".format(time.strftime("%Y-%m-%d %H:%M", time.localtime(int(acc["created_ts"])))),
        "最后登录：{}".format(time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(int(acc.get("last_login_ts") or acc["created_ts"])))),
        "登录次数：{}".format(acc.get("login_count", 0)),
    ]
    chat = "{} {} 登录{}次".format(acc["username"], PERM_NAMES.get(perm, "?"), acc.get("login_count", 0))
    return ok(lines, chat=chat)


@register("server", aliases=("服",), args="[info]", desc="服务器状态", group="基础")
def cmd_server(ctx, target, *args):
    st = db.stats()
    gs = gamedata.stats()
    online = len(db.online_accounts())
    lines = [
        '服务器：{}'.format(ctx.server_name or "OpenOrange"),
        '账号：{}'.format(st.get("accounts", 0)),
        '在线：{}'.format(online),
        '票据：{}'.format(st.get("tickets", 0)),
        '物品索引：{} 条'.format(gs["items"]),
        '角色索引：{} 条'.format(gs["chars"]),
    ]
    chat = '{} 账号{} 在线{}'.format(ctx.server_name or "OpenOrange", st.get("accounts", 0), online)
    return ok(lines, chat=chat)


@register("clear", aliases=("清屏",), desc="清空网页控制台输出", group="基础")
def cmd_clear(ctx, target, *args):
    return ok([], chat="", data={"clear": True})


# ---------------------------------------------------------------- GM 命令
#
# 数据侧已经全部打通：ID / 名字解析、参数校验、完整信息展示，用的都是
# BDD（pmdata.bin）里的真表 —— 物品 2728、角色 193（可玩 29）、
# 货币 8、活动 227、任务 465、地图 32、卡池 84、剧情 2401。
#
# 生效侧还要等游戏端 RPC 接入（ROADMAP 1-1 / 1-2 / 2-3 / 3-3），
# 所以每条命令现在都会：解析 → 校验 → 说明将要发生什么，
# 并明确标注哪一步还没接。

