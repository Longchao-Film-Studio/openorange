# -*- coding: utf-8 -*-
"""唯一序列化器：变更集 -> propertySync 帧（P0 地基 B3）。

## 职责

1. **排序**：先父后子（新建的容器必须先发，否则客户端没有父对象的 __id__）
2. **剔除私有字段**：递归剥掉 _oo_* 前缀的键
3. **断言**：OP_ADD 的值必须带 __id__（客户端 __OpAdd__ 会 assert）
4. **编码**：交给 ms.build_property_sync（msgpack + "#N" 整数键转换）

## 为什么单独一层

审计发现两个真实 bug：

- ms._live_init_dict 的 _oo_ 过滤**只过滤顶层**，嵌套层的 _oo_* 会原样发给客户端，
  客户端 initProperty 里 assert(customType ~= nil) 直接崩
- 各处手拼帧时没人检查「OP_ADD 的值有没有 __id__」

这两个检查放在这一层，所有走 Tx 的地方自动受益。
"""
from .tx import OP_DEL, OP_REMOVE, OP_ADD


def _strip_private(value):
    """递归剥掉 _oo_* 前缀的键。返回新对象（不改原值）。"""
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items()
                if not (isinstance(k, str) and k.startswith("_oo_"))}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_private(v) for v in value)
    return value


def _sort_key(op):
    """排序：路径短的在前（先父后子），同长度按路径原顺序。"""
    path = op.path or ()
    return (len(path), tuple(str(x) for x in path))


def validate(ops):
    """开发期断言。返回错误信息列表（空表示通过）。"""
    errs = []
    for op in ops:
        if op.optype == OP_ADD:
            if not isinstance(op.value, dict):
                errs.append("OP_ADD %r 的值不是 dict" % (op.path,))
            elif not isinstance(op.value.get("__id__"), int):
                errs.append("OP_ADD %r 的值没有 __id__" % (op.path,))
        if op.optype in (OP_DEL, OP_REMOVE):
            if op.value is not None:
                errs.append("OP_DEL %r 不该带值" % (op.path,))
        if not isinstance(op.property_id, int):
            errs.append("%r 的 property_id 不是整数" % (op.path,))
    return errs


def ops_to_frames(ops, strict=False):
    """把 Tx 的 ops 编成 propertySync 帧列表。

    strict=True 时，validate 发现错误直接抛异常（测试用）；
    生产路径用 strict=False，只在控制台打印警告，不中断 RPC。
    """
    if not ops:
        return []

    errs = validate(ops)
    if errs:
        if strict:
            raise ValueError("变更集校验失败：%s" % "; ".join(errs))
        for e in errs:
            print("[tx] ⚠ 变更集校验: %s" % e, flush=True)

    from .. import ms as _ms

    frames = []
    for op in sorted(ops, key=_sort_key):
        if op.optype in (OP_DEL, OP_REMOVE):
            value = None
        else:
            value = _strip_private(op.value)
        frames.append(_ms.build_property_sync(
            "", op.property_id, op.optype, op.name, value))
    return frames
