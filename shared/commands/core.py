"""指令系统核心：权限 / 结果 / 上下文 / 注册表 / 解析 / 执行。

从 shared/commands.py 拆出（P0 遗留第 2 条）。这一层不含任何具体命令，
只负责「一条 /xxx 文本怎么变成 Result」：

  权限      PERM_PLAYER / PERM_GM / PERM_ADMIN
  结果      Result + ok() / err()（lines 给网页控制台，chat 给游戏内聊天框）
  上下文    Context（谁在什么地方执行、能不能跨账号操作）
  注册表    COMMANDS / ALIASES / register() / lookup()
  解析执行  split_tokens -> Target -> resolve_target -> execute
"""
from shared import db

from .backend import admin_call as _admin
from .backend import admin_down

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


class Target(str):
    """命令目标 —— 把「目标账号」这件事收成一个抽象。

    为什么继承 str：
      Target 的值就是账号名，所以现有命令里所有 `"%s" % target`、
      `db.get_account_by_username(target)`、`target != ctx.actor` 全都不用改。
      需要富行为时再用下面这些属性和方法。

    以前每个命令都在重复做：
      - str(target) 转换
      - 判断是不是自己
      - 自己判断管理员权限（和 execute() 里的重复）
      - 手拼 /player/<uid>/xxx 去调游戏服管理接口
    现在统一收进这里。

    用法：
      def cmd_xxx(ctx, target, *args):
          target.uid                      # 游戏内 uid（本项目复用账号名）
          target.is_self                  # 是不是 @ 自己
          target.account                  # 账号记录（dict 或 None）
          target.require_admin()          # 跨账号操作校验，返回 err 或 None
          player, e = target.player("story")   # 读游戏服状态，返回 (dict, err)
    """

    __slots__ = ("_ctx", "_is_self")

    def __new__(cls, username, ctx=None, is_self=False):
        obj = super().__new__(cls, username or "")
        obj._ctx = ctx
        obj._is_self = bool(is_self)
        return obj

    @property
    def uid(self):
        """游戏内 uid。本项目 uid 直接复用账号名。"""
        return str(self)

    @property
    def is_self(self):
        """是不是 @ 自己。"""
        return self._is_self

    @property
    def perm(self):
        """目标账号的权限等级。"""
        try:
            return db.get_perm_level(str(self))
        except Exception:
            return PERM_PLAYER

    @property
    def account(self):
        """目标账号记录（dict 或 None）。"""
        try:
            return db.get_account_by_username(str(self))
        except Exception:
            return None

    def require_admin(self):
        """跨账号操作需要管理员。返回 err 或 None。

        注意：execute() 在派发前已经统一校验过一次；这里是给
        「即使目标是自己、但操作有额外风险」的命令做二次校验用的。
        """
        if self._is_self:
            return None
        if self._ctx is None or self._ctx.perm >= PERM_ADMIN:
            return None
        return err("只有管理员才能对别的账号操作",
                   chat="✗ 需要管理员权限")

    def player(self, kind):
        """读游戏服上该账号的状态（走管理接口）。

        kind: "story" | "dungeon"
        返回 (player_dict, err)。
        """
        info, e = _admin("GET", "/player/%s/%s" % (self.uid, kind))
        if e:
            return None, admin_down()
        if not info or not info.get("ok"):
            return None, "查询失败: %s" % (info,)
        return (info.get("player") or {}), None

    def call(self, method, path, payload=None):
        """以该账号为目标调游戏服管理接口。

        自动把 uid 塞进 payload（如果 payload 是 dict）。
        """
        body = dict(payload or {})
        body.setdefault("uid", self.uid)
        return _admin(method, path, body)

    def __repr__(self):
        return "Target(%r%s)" % (str(self), ", self" if self._is_self else "")


def resolve_target(ctx, token):
    """解析 target。返回 (Target, error_result)。"""
    token = (token or "").strip()
    # 只保留 "@" 作为"自己"：它不可能成为账号名（注册规则只允许字母/数字/下划线）。
    # 之前还接受 "self"/"自己"/"我"，但 "self" 恰好是合法账号名，
    # 会把这个账号永久遮蔽成"自己"，导致管理员无法对它执行任何命令。
    if token == "@" or not token:
        return Target(ctx.actor, ctx, True), None
    if db.get_account_by_username(token) is None:
        return None, err("找不到账号「%s」" % token,
                         chat="✗ 找不到账号 %s" % token)
    return Target(token, ctx, token == ctx.actor), None




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


