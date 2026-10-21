#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""默认 uid / 实体 id（P0-E3 从 ms.py 拆出）。

★ 这些值必须从 config 取，不要在这里写死端口/uid/实体 id
  （踩过：写死后换账号/换 uid 就对不上，且 initDict 与 entity 路由不一致）。
"""
try:
    from .. import config as ooconf
except Exception:  # 允许脱离包单独跑（历史脚本这么用过）
    import config as ooconf  # type: ignore

_DEF_UID = ooconf.FALLBACK_UID
_DEF_AVATAR_EID = ooconf.avatar_entity_id(ooconf.FALLBACK_UID)
_DEF_ACCOUNT_EID = ooconf.account_entity_id(ooconf.FALLBACK_UID)
