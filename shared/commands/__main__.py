# -*- coding: utf-8 -*-
"""python -m shared.commands —— 手动敲几条命令看输出（开发自检用）。

以前这段写在 __init__.py 里，靠 __name__ == "__main__" 触发；
但包被 import 时永远走不到，而 python -m shared.commands 又需要 __main__.py，
两边都不成立。挪到这里才是可达的。
"""
import sys

from shared import db

from . import COMMANDS, PERM_ADMIN, Context, execute

from shared.log_tee import get_logger
_log = get_logger(__name__)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    db.init()
    ctx = Context("aixiaoji", PERM_ADMIN, source="cli")
    for line in ["/help", "/who", "/server", "/search 长命锁",
                 "/give @ 1001 1", "/account aixiaoji", "/badcmd"]:
        r = execute(line, ctx)
        _log.info("=" * 60)
        _log.info("> " + line)
        _log.info(r.text or "(空)")
        _log.info("[chat] " + r.chat)
    _log.info("已注册命令数: %d" % len(COMMANDS))


if __name__ == "__main__":
    main()
