#!/usr/bin/env python3
"""邮件系统（MailService）。

分层（依赖方向单向，不许回指）：

    rpc.py       协议层：把 MsResponse 的 msgpack 拼出来。只有它知道字段名
      ↓
    send.py      发信：模板 -> 实例。所有发信方的唯一入口
      ↓
    template.py  配表：BDD MailData -> 实例字段（含 U2 有效期的唯一口径）
    store.py     存储：props["_oo_mail"] 的三个写入口 + 只读查询
"""
from . import send, store, template

__all__ = ["send", "store", "template"]
