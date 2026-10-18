# -*- coding: utf-8 -*-
"""OpenOrange 共享数据层。

web 与 game_server 都直接 import 本包, 读写同一个 SQLite(data.db)。
"""
from . import db  # noqa: F401

__all__ = ["db"]
