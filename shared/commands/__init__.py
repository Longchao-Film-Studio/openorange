# -*- coding: utf-8 -*-
"""shared.commands —— 指令系统（P0 遗留第 2 条：按命令组拆分）。

语法:
    /<命令> <target> <参数...>

    target:
        @          自己
        <账号名>    指定账号（需要管理员权限）

输出:
    每条命令返回 Result，内含两套输出：
        lines  网页控制台用（完整、可多行）
        chat   游戏内聊天框用（单行、紧凑）

分层:
    core.py       权限 / 结果 / 上下文 / 注册表 / 解析 / 执行
    backend.py    游戏服管理接口 HTTP 客户端 + BACKEND_HINT
    player.py     /help /who /search /account /server /clear
    game.py       /give /char /lv /money /tp /quest /story /act
    admin_cmds.py /dungeon /prologue

★ 命令靠 import 时的 @register 装饰器注册，所以下面必须 import 各命令模块。
"""
from .core import *  # noqa: F401,F403
from .core import (ALIASES, COMMANDS, Command, Context, ParseError,  # noqa: F401
                   PERM_ADMIN, PERM_GM, PERM_NAMES, PERM_PLAYER, Result,
                   Target, _ambiguous, _disp_width, _need_num, _pad, err,
                   execute, lookup, ok, register, resolve_target,
                   split_tokens)
from .backend import (ADMIN_TOKEN, ADMIN_URL, BACKEND_HINT,  # noqa: F401
                      admin_call, admin_down)
from . import player, game, admin_cmds  # noqa: F401  导入即注册命令
from .player import (cmd_account, cmd_clear, cmd_help,  # noqa: F401
                     cmd_search, cmd_server, cmd_who)
from .game import (cmd_act, cmd_char, cmd_give, cmd_lv,  # noqa: F401
                   cmd_money, cmd_quest, cmd_story, cmd_tp)
from .admin_cmds import cmd_dungeon, cmd_prologue  # noqa: F401


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
