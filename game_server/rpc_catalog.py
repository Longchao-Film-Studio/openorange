#!/usr/bin/env python3
"""RPC 名字反查（未实现请求落盘 / 黑名单用）。

数据源
------
work/rpc_list.tsv —— tools/gen_rpc_list.py 从客户端 Lua 扫描生成：
    direction  name  index  implemented  params  sites
    C2S 572 条 + S2C 147 条

为什么不直接用 rpc_index.RPC_INDEX
----------------------------------
RPC_INDEX 只是「我们**已登记**」的 111 条。客户端实际会发 572 条 C2S，
其余 461 条在服务端是裸 index —— 日志里只会看到 `index:123456789`，
既没法读，也没法按名字做安全判断（比如经济类 RPC 绝不能回成功）。

★ 本模块只读，不改任何行为；名字缺失时优雅返回 None。
"""
import os
import threading

_TSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "work", "rpc_list.tsv")

_LOCK = threading.RLock()
_BY_INDEX = None
_BY_NAME = None


def _load():
    global _BY_INDEX, _BY_NAME
    with _LOCK:
        if _BY_INDEX is not None:
            return
        by_index, by_name = {}, {}
        try:
            with open(_TSV, "r", encoding="utf-8", errors="replace") as f:
                next(f, None)                      # 表头
                for line in f:
                    row = line.rstrip("\r\n").split("\t")
                    if len(row) < 3:
                        continue
                    name, index = row[1].strip(), row[2].strip()
                    if not name or not index.isdigit():
                        continue
                    idx = int(index)
                    by_index.setdefault(idx, name)
                    by_name.setdefault(name, idx)
        except OSError:
            pass
        _BY_INDEX, _BY_NAME = by_index, by_name


def name_of(index):
    """索引 -> 名字；查不到返回 None。"""
    _load()
    with _LOCK:
        return _BY_INDEX.get(int(index)) if _BY_INDEX else None


def index_of(name):
    """名字 -> 索引；查不到返回 None。"""
    _load()
    with _LOCK:
        return _BY_NAME.get(str(name)) if _BY_NAME else None


def size():
    _load()
    with _LOCK:
        return len(_BY_INDEX or ())
