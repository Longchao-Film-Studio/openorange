#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared.commands —— 指令系统

语法:
    /<命令> <target> <参数...>

    target:
        @          自己
        <账号名>    指定账号（需要管理员权限）

输出:
    每条命令返回 Result，内含两套输出：
        lines  网页控制台用（完整、可多行）
        chat   游戏内聊天框用（单行、紧凑）

命令注册:
    @register("give", args="<target> <item> [num]", perm=PERM_GM, desc="给物品")
    def cmd_give(ctx, target, item, num="1"):
        ...
"""
import io
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db, gamedata  # noqa: E402

# ---------------------------------------------------------------- 权限
PERM_PLAYER = 0
PERM_GM = 1
PERM_ADMIN = 2

PERM_NAMES = {PERM_PLAYER: "玩家", PERM_GM: "GM", PERM_ADMIN: "管理员"}


# ---------------------------------------------------------------- 结果
class Result:
    """一条命令的执行结果。

    lines: 网页输出（多行）
    chat:  游戏内输出（单行，建议 <= 60 字）
    data:  结构化数据，供前端做额外渲染
    """

    __slots__ = ("ok", "lines", "chat", "data")

    def __init__(self, ok=True, lines=None, chat=None, data=None):
        self.ok = ok
        self.lines = list(lines or [])
        if chat is None:
            chat = self.lines[0] if self.lines else ""
        self.chat = chat
        self.data = data or {}

    @property
    def text(self):
        return "\n".join(self.lines)


def ok(lines, chat=None, data=None):
    return Result(True, lines, chat, data)


def err(msg, chat=None, data=None):
    return Result(False, [msg], chat, data)


# ---------------------------------------------------------------- 上下文
class Context:
    """一次命令执行的上下文。"""

    __slots__ = ("actor", "perm", "source", "server_name", "allow_unavailables")

    def __init__(self, actor, perm=PERM_PLAYER, source="web", server_name="",
                 allow_unavailables=False):
        self.actor = actor
        self.perm = int(perm or 0)
        self.source = source
        self.server_name = server_name or ""
        self.allow_unavailables = bool(allow_unavailables)


# ---------------------------------------------------------------- 命令定义
class Command:
    __slots__ = ("name", "handler", "aliases", "args", "desc", "perm",
                 "needs_target", "implemented", "group")

    def __init__(self, name, handler, aliases=(), args="", desc="",
                 perm=PERM_PLAYER, needs_target=False, implemented=True, group="其它"):
        self.name = name
        self.handler = handler
        self.aliases = tuple(aliases)
        self.args = args
        self.desc = desc
        self.perm = perm
        self.needs_target = needs_target
        self.implemented = implemented
        self.group = group


COMMANDS = {}
ALIASES = {}


def register(name, aliases=(), args="", desc="", perm=PERM_PLAYER,
             needs_target=False, implemented=True, group="其它"):
    def deco(fn):
        cmd = Command(name, fn, aliases=aliases, args=args, desc=desc, perm=perm,
                      needs_target=needs_target, implemented=implemented, group=group)
        COMMANDS[name] = cmd
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


def lookup(name):
    name = (name or "").strip().lower()
    if name in COMMANDS:
        return COMMANDS[name]
    if name in ALIASES:
        return COMMANDS[ALIASES[name]]
    return None


# ---------------------------------------------------------------- 解析
class ParseError(Exception):
    pass


def split_tokens(text):
    """按空白切分。同时兼容中文全角空格。"""
    out = []
    cur = []
    for ch in text:
        if ch.isspace() or ch == "\u3000":
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def resolve_target(ctx, token):
    """解析 target。返回 (username, error_result)。"""
    token = (token or "").strip()
    # 只保留 "@" 作为"自己"：它不可能成为账号名（注册规则只允许字母/数字/下划线）。
    # 之前还接受 "self"/"自己"/"我"，但 "self" 恰好是合法账号名，
    # 会把这个账号永久遮蔽成"自己"，导致管理员无法对它执行任何命令。
    if token == "@":
        return ctx.actor, None
    if not token:
        return ctx.actor, None
    if db.get_account_by_username(token) is None:
        return None, err("找不到账号「%s」" % token,
                         chat="✗ 找不到账号 %s" % token)
    return token, None


def execute(text, ctx):
    """执行一条命令文本。永远返回 Result，不抛异常。"""
    raw = (text or "").strip()
    if not raw:
        return err("请输入命令，/help 查看全部", chat="✗ 请输入命令")

    if not raw.startswith("/"):
        return err("命令必须以 / 开头，例如 /help",
                   chat="✗ 命令要以 / 开头")

    body = raw[1:].strip()
    if not body:
        return err("请输入命令，/help 查看全部", chat="✗ 请输入命令")

    parts = split_tokens(body)
    cmd = lookup(parts[0])
    if cmd is None:
        return err("未知命令「%s」，/help 查看全部" % parts[0],
                   chat="✗ 未知命令 %s" % parts[0])

    if cmd.perm > ctx.perm:
        return err("权限不足：该命令需要「%s」" % PERM_NAMES.get(cmd.perm, "?"),
                   chat="✗ 权限不足")

    rest = parts[1:]

    # 全局开关：--allow-unavailables 取消「优先取可用条目」的规则
    allow_flags = ("--allow-unavailables", "--allow-unavailable", "-a")
    if any(tok in allow_flags for tok in rest):
        ctx.allow_unavailables = True
        rest = [tok for tok in rest if tok not in allow_flags]

    target = ctx.actor
    if cmd.needs_target:
        if not rest:
            return err("缺少目标，用法：/%s %s" % (cmd.name, cmd.args),
                       chat="✗ 用法: /%s %s" % (cmd.name, cmd.args))
        target, e = resolve_target(ctx, rest[0])
        if e is not None:
            return e
        rest = rest[1:]
        if target != ctx.actor and ctx.perm < PERM_ADMIN:
            return err("只有管理员才能对别的账号操作",
                       chat="✗ 需要管理员权限")

    try:
        return cmd.handler(ctx, target, *rest)
    except Exception as ex:  # 命令实现里的 bug 不应拖垮服务
        return err("命令执行异常：%s: %s" % (type(ex).__name__, ex),
                   chat="✗ 命令异常")


# ---------------------------------------------------------------- 输出对齐
def _disp_width(s):
    """字符串的显示宽度：中日韩全角字符按 2 列算。"""
    import unicodedata
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _disp_width(s))


# ---------------------------------------------------------------- 命令实现
@register("help", aliases=("?", "帮助"), args="[命令]", desc="查看命令列表或某条命令的详情", group="基础")
def cmd_help(ctx, target, *args):
    if args:
        cmd = lookup(args[0])
        if cmd is None:
            return err("没有命令「%s」" % args[0], chat="✗ 没有命令 %s" % args[0])
        lines = [
            "/%s %s" % (cmd.name, cmd.args),
            "  %s" % cmd.desc,
            "  权限: %s%s" % (PERM_NAMES.get(cmd.perm, "?"),
                            "" if cmd.implemented else "   [尚未实现]"),
        ]
        if cmd.aliases:
            lines.append("  别名: %s" % "、".join("/" + a for a in cmd.aliases))
        return ok(lines, chat="看 %s: %s" % (cmd.name, cmd.desc))

    groups = {}
    for cmd in COMMANDS.values():
        groups.setdefault(cmd.group, []).append(cmd)

    order = ["基础", "查询", "账号", "待实现"]
    names = order + [g for g in sorted(groups) if g not in order]

    lines = []
    for gname in names:
        if gname not in groups:
            continue
        lines.append("【%s】" % gname)
        for cmd in sorted(groups[gname], key=lambda c: c.name):
            mark = "" if cmd.implemented else "  (未实现)"
            lines.append("  /%s %s%s" % (_pad(cmd.name, 9), _pad(cmd.args, 30), cmd.desc + mark))
        lines.append("")
    lines.append("语法：/命令 <target> <参数>，target 用 @ 表示自己")
    lines.append("同名条目默认取「可用」的那条（可玩角色 / 给角色的物品）；")
    lines.append("加 --allow-unavailables 可取消该规则，改为返回全部候选。")
    chat = "共 %d 条命令，网页 /admin/command 查看完整列表" % len(COMMANDS)
    return ok(lines, chat=chat)


@register("who", aliases=("在线",), desc="列出在线玩家", group="基础")
def cmd_who(ctx, target, *args):
    # 只统计**真正进了游戏**的连接（game_online 表，由 game_server 维护）。
    # 以前读的是 sessions（网页会话），启动器每登录一次留一条，
    # 过期时间又长，结果 /who 把历史登录全列出来（同一个账号 8 条）。
    rows = db.list_game_online()
    if not rows:
        return ok(["当前没有游戏内在线玩家。"], chat="在线 0 人")
    lines = ["在线 %d 人：" % len(rows)]
    for r in rows:
        name = r.get("username") or "?"
        perm = PERM_NAMES.get(int(r.get("perm_level") or 0), "?")
        uid = r.get("uid") or "-"
        addr = r.get("addr") or "-"
        since = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("since_ts") or 0)))
        lines.append("  %-16s %-8s uid=%-12s %s  上线 %s" % (name, perm, uid, addr, since))
    chat = "在线 %d 人: %s" % (len(rows), " ".join(r.get("username") or "?" for r in rows[:4]))
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
        return err("没有匹配「%s」的物品或角色" % keyword,
                   chat="✗ 没有匹配 %s" % keyword)

    lines = ["搜「%s」：物品 %d 条，角色 %d 条" % (keyword, len(items), len(chars))]

    if chars:
        lines.append("")
        lines.append("角色 (%d)" % len(chars))
        for ch in chars[:20]:
            lines.append("  %-7d %s %-8s %s星  %s" % (
                ch["id"], _pad(gamedata.tier_name(ch), 4), ch["name"],
                ch.get("grade") or "-", ch.get("area") or ""))

    if items:
        lines.append("")
        lines.append("物品 (%d)" % len(items))
        for it in items[:30]:
            lines.append("  %-7d %s %-20s type=%s quality=%s" % (
                it["id"], _pad(gamedata.tier_name(it), 4), it["name"],
                it.get("type"), it.get("quality")))

    # 游戏内：单行紧凑 + 分页
    flat = [("角色", c["id"], c["name"]) for c in chars] + \
           [("物品", i["id"], i["name"]) for i in items]
    start = (page - 1) * PAGE_SIZE
    chunk = flat[start:start + PAGE_SIZE]
    if not chunk:
        return ok(lines, chat="共 %d 条，没有第 %d 页" % (total, page))
    parts = ["%d%s" % (cid, name) for _, cid, name in chunk]
    chat = "共%d条 %d-%d: %s" % (total, start + 1, start + len(chunk), " ".join(parts))
    if len(flat) > start + PAGE_SIZE:
        chat += "  /search %s %d" % (keyword, page + 1)
    return ok(lines, chat=chat, data={"total": total, "page": page})


@register("account", aliases=("账号",), args="<target> [info|perm <0-2>]",
          desc="查看账号信息 / 设置权限（0=玩家 1=GM 2=管理员）",
          perm=PERM_PLAYER, needs_target=True, group="账号")
def cmd_account(ctx, target, *args):
    acc = db.get_account_by_username(target)
    if not acc:
        return err("找不到账号「%s」" % target, chat="✗ 找不到账号 %s" % target)

    sub = (args[0].lower() if args else "info")

    if sub == "perm":
        if ctx.perm < PERM_ADMIN:
            return err("只有管理员能改权限", chat="✗ 需要管理员权限")
        if len(args) < 2:
            return err("用法：/account %s perm <0|1|2>" % target,
                       chat="✗ 用法: /account 名 perm 0|1|2")
        try:
            level = int(args[1])
        except ValueError:
            return err("权限等级必须是 0 / 1 / 2", chat="✗ 权限要是 0/1/2")
        if level not in PERM_NAMES:
            return err("权限等级必须是 0 / 1 / 2", chat="✗ 权限要是 0/1/2")
        db.set_perm_level(target, level)
        return ok(["已把「%s」的权限设为 %d（%s）" % (target, level, PERM_NAMES[level])],
                  chat="✓ %s -> %s" % (target, PERM_NAMES[level]))

    perm = int(acc.get("perm_level") or 0)
    lines = [
        "账号：%s" % acc["username"],
        "账号 ID：%s" % acc["account_id"],
        "权限：%d（%s）" % (perm, PERM_NAMES.get(perm, "?")),
        "注册：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(int(acc["created_ts"]))),
        "最后登录：%s" % time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(int(acc.get("last_login_ts") or acc["created_ts"]))),
        "登录次数：%s" % acc.get("login_count", 0),
    ]
    chat = "%s %s 登录%s次" % (acc["username"], PERM_NAMES.get(perm, "?"), acc.get("login_count", 0))
    return ok(lines, chat=chat)


@register("server", aliases=("服",), args="[info]", desc="服务器状态", group="基础")
def cmd_server(ctx, target, *args):
    st = db.stats()
    gs = gamedata.stats()
    online = len(db.online_accounts())
    lines = [
        "服务器：%s" % (ctx.server_name or "OpenOrange"),
        "账号：%d" % st.get("accounts", 0),
        "在线：%d" % online,
        "票据：%d" % st.get("tickets", 0),
        "物品索引：%d 条" % gs["items"],
        "角色索引：%d 条" % gs["chars"],
    ]
    chat = "%s 账号%d 在线%d" % (ctx.server_name or "OpenOrange", st.get("accounts", 0), online)
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

BACKEND_HINT = "游戏端 RPC 尚未接入，本次只解析校验、未实际生效"


def _ambiguous(kind, cands):
    """重名时的统一提示。"""
    ids = [str(c.get("id")) for c in cands[:12]]
    tail = " …" if len(cands) > 12 else ""
    return err("「%s」有 %d 条同名记录，请用 ID 指定：%s%s"
               % (kind, len(cands), "、".join(ids), tail),
               chat="✗ 重名 %d 条，用 ID 指定" % len(cands))


def _need_num(tok, default=None, lo=None, hi=None, label="数量"):
    if tok is None:
        return default, None
    try:
        v = int(tok)
    except (TypeError, ValueError):
        return None, err("%s必须是整数：%s" % (label, tok))
    if lo is not None and v < lo:
        return None, err("%s不能小于 %d" % (label, lo))
    if hi is not None and v > hi:
        return None, err("%s不能大于 %d" % (label, hi))
    return v, None


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


# ---------------------------------------------------------------- 副本调试

# 运行时开关（存在共享库 settings 表里，网页端写、游戏服读）
DUNGEON_FLAG = "dungeon_last_only"

_ONOFF = {
    "on": True, "off": False,
    "1": True, "0": False,
    "true": True, "false": False,
    "开": True, "关": False,
}


@register("dungeon", aliases=("副本",), args="[last-only on|off]",
          desc="副本调试：只下发最后一关（一关直达 Boss）",
          perm=PERM_GM, needs_target=False, group="GM")
def cmd_dungeon(ctx, target, *args):
    """副本调试开关。

    背景：序章 guide_01 有 3 关（50001 天台 / 50002 千相会 / 50003 千手Boss战），
    测结算时每次打三关太慢。客户端 dispatchhubonline.lua:49-59 优先用
    服务端下发的 spaceInfo.levelIds，所以只给最后一关就能一关直达 Boss。

    开关存在 shared/db 的 settings 表，game_server 每次进副本都会读，
    所以这里改完**立即生效，不用重启游戏服**。
    """
    if not args:
        cur = db.get_setting_bool(DUNGEON_FLAG, False)
        lines = [
            "副本设置",
            "  只下发最后一关    %s" % ("开启" if cur else "关闭"),
            "",
            "  序章 guide_01 的关卡：",
            "    50001  序章_中浦区天台",
            "    50002  序章_千相会龙域",
            "    50003  序章_千手Boss战",
            "",
            "  开启后进副本只给 50003，测结算不用打三关。",
            "",
            "  用法：/dungeon last-only on|off",
            "  生效：立即（游戏服每次进副本都读共享库）",
        ]
        return ok(lines,
                  chat="副本: 只下发最后一关 %s" % ("开" if cur else "关"),
                  data={"action": "dungeon_show", "last_only": cur})

    sub = (args[0] or "").strip().lower()
    if sub not in ("last-only", "lastonly", "last", "最后一关"):
        return err("未知子命令「%s」，用法：/dungeon last-only on|off" % args[0],
                   chat="✗ 用法: /dungeon last-only on|off")
    if len(args) < 2:
        return err("缺少开关，用法：/dungeon last-only on|off",
                   chat="✗ 用法: /dungeon last-only on|off")
    val = _ONOFF.get((args[1] or "").strip().lower())
    if val is None:
        return err("开关只能是 on / off", chat="✗ 只能是 on/off")

    db.set_setting(DUNGEON_FLAG, 1 if val else 0)
    lines = [
        "✔ 已设置",
        "  只下发最后一关    %s" % ("开启" if val else "关闭"),
        "",
        "  立即生效：游戏服每次进副本都会读共享库，不用重启。",
    ]
    return ok(lines,
              chat="✔ 副本: 只下发最后一关 %s" % ("开" if val else "关"),
              data={"action": "dungeon_set", "last_only": val, "applied": True})


# ---------------------------------------------------------------- 序章跳过

SKIP_PROLOGUE_PREFIX = "skip_prologue:"


@register("prologue", aliases=("序章",), args="[skip]",
          desc="序章：查看状态 / 跳过（跳过 = 直接进街区，不再打副本）",
          perm=PERM_GM, needs_target=False, group="GM")
def cmd_prologue(ctx, target, *args):
    """查看或跳过序章。

    跳过是**一次性指令**：命令往共享库写 skip_prologue:<uid>=1，
    游戏服在下一次登录（ms._live_init_dict）时看到就：
      - StoryTask 1000 标 Finished
      - StoryTask 1003 设 Doing
      - story_finished[1] = True
    然后把标记清掉。这样不用打序章副本就能直接进街区。

    真正打副本的路径不受影响：那是由结算 RPC 推进的。
    """
    key = SKIP_PROLOGUE_PREFIX + str(target)
    pending = db.get_setting_bool(key, False)

    if not args:
        lines = [
            "序章状态  账号 %s" % target,
            "  待跳过的标记    %s" % ("有（下次登录生效）" if pending else "无"),
            "",
            "  跳过之后：StoryTask 1000 标完成、1003 开始、直接进街区。",
            "  不跳过：新号第一次登录会进序章副本（3 关，可用 /dungeon last-only 只打 Boss）。",
            "",
            "  用法：/prologue skip",
        ]
        return ok(lines,
                  chat="序章 %s: %s" % (target, "待跳过" if pending else "未跳过"),
                  data={"action": "prologue_show", "target": target,
                        "pending": pending})

    sub = (args[0] or "").strip().lower()
    if sub not in ("skip", "跳过"):
        return err("未知子命令「%s」，用法：/prologue skip" % args[0],
                   chat="✗ 用法: /prologue skip")

    db.set_setting(key, 1)
    lines = [
        "✔ 已标记跳过序章",
        "  账号    %s" % target,
        "",
        "  生效时机：该账号**下一次登录**（游戏服读共享库后应用）。",
        "  应用后：StoryTask 1000 → 完成、1003 → 进行中、story_finished[1] = True。",
        "",
        "  ⚠ 只在服务端生效，客户端要重新登录才会看到。",
    ]
    return ok(lines,
              chat="✔ 序章跳过已标记（%s 下次登录生效）" % target,
              data={"action": "prologue_skip", "target": target,
                    "applied": True})


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db.init()
    ctx = Context("aixiaoji", PERM_ADMIN, source="cli")
    for line in ["/help", "/who", "/server", "/search 长命锁",
                 "/give @ 1001 1", "/account aixiaoji", "/badcmd"]:
        r = execute(line, ctx)
        print("=" * 60)
        print("> " + line)
        print(r.text or "(空)")
        print("[chat] " + r.chat)
