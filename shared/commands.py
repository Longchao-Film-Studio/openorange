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
    if token in ("@", "self", "自己", "我"):
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
    rows = db.online_accounts()
    if not rows:
        return ok(["当前没有网页在线玩家。"], chat="在线 0 人")
    lines = ["在线 %d 人：" % len(rows)]
    for r in rows:
        name = r["username"] or "?"
        perm = PERM_NAMES.get(int(r["perm_level"] or 0), "?")
        lines.append("  %-16s %-8s %s" % (name, perm,
                                          time.strftime("%m-%d %H:%M", time.localtime(int(r["issued_ts"])))))
    chat = "在线 %d 人: %s" % (len(rows), " ".join(r["username"] or "?" for r in rows[:4]))
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


# ---------------------------------------------------------------- 待实现命令
def _not_implemented(reason):
    def handler(ctx, target, *args):
        return err("该命令尚未实现：%s" % reason,
                   chat="✗ 尚未实现")
    return handler


for _name, _args, _desc, _why in [
    ("give", "<target> <item> [num]", "给物品", "依赖背包系统（ROADMAP 1-2）"),
    ("char", "<target> <char>", "给角色", "依赖角色系统（ROADMAP 1-1）"),
    ("lv", "<target> <level>", "设置玩家等级", "依赖玩家数据（ROADMAP 1-3）"),
    ("money", "<target> <type> <num>", "增加货币", "依赖背包系统（ROADMAP 1-2）"),
    ("tp", "<target> <map>", "传送", "依赖场景加载（ROADMAP 0-4）"),
    ("quest", "<target> <taskId>", "完成任务", "依赖任务系统（ROADMAP 3-4）"),
    ("story", "<target> <storyId>", "播放剧情", "依赖剧情触发（ROADMAP 2-3）"),
    ("act", "<id> <open|close>", "开关活动", "依赖活动系统（ROADMAP 3-3）"),
]:
    register(_name, args=_args, desc=_desc, perm=PERM_GM,
             needs_target=(_name != "act"),
             implemented=False, group="待实现")(
        _not_implemented(_why))


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
