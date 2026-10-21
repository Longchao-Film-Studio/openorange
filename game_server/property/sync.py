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


def _strip_private(value, is_root=False):
    """递归剥掉私有键。返回新对象（不改原值）。

    剥两类：
      _oo_*  —— 服务端私有字段，任何层级都不该发给客户端
      __tp__ —— **只在根属性值里保留**；嵌套值一律剥掉

    ★ 为什么嵌套值必须剥 __tp__（2026-10-21 审计 P0-1/P0-2 实锤）：
      客户端 initProperty 用 __tp__ 决定「这个根属性是什么自定义类型」；
      但嵌套路径的 roinit/rochange（customdict.lua / customlist.lua）
      会把它当成一个**普通子键**去解析：
        - 结构体（StreetInfo）没有 __ValueTypeDeclare__ ->
          rochange 里 valueTypeDeclare:isCustomType() 索引 nil 崩
        - 容器（TaskBag）有 __ValueType__=TaskItem ->
          把 "TaskBag" 这个字符串当 TaskItem 解析 -> index a string value 崩
      实测：声望增长 / 宝箱首开 / 接通用任务 三条路径都会发这种帧。
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("_oo_"):
                continue
            if k == "__tp__" and not is_root:
                continue
            out[k] = _strip_private(v, is_root=False)
        return out
    if isinstance(value, list):
        return [_strip_private(v, is_root=False) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_private(v, is_root=False) for v in value)
    return value


def _sort_key(op):
    """排序：路径短的在前（先父后子），同长度按路径原顺序。"""
    path = op.path or ()
    return (len(path), tuple(str(x) for x in path))


def validate(ops):
    """开发期断言。返回错误信息列表（空表示通过）。

    ★ 2026-10-21 审计 P0-3：原来只查「OP_ADD 是 dict 且有 __id__」，
      对 8 类客户端会崩的场景里 7 类返回 []。现在补上：
        - OP_ADD 的 propertyId 必须 > 0（客户端要拿它找父对象）
        - 同一批 ops 里不能有重复的 __id__（客户端 id repeated! 覆盖缓存）
        - 值里嵌套的 dict 必须带 __id__（客户端 roinit assert）
        - 结构体字段禁止 OP_ADD（客户端 __OpAdd__ 要求 __ValueTypeDeclare__）
        - list 键禁止 OP_DEL（应用 OP_REMOVE）
    """
    from . import spec as _spec
    errs = []
    seen_ids = {}

    def walk_ids(obj, path):
        if isinstance(obj, dict):
            v = obj.get("__id__")
            if isinstance(v, int) and not isinstance(v, bool):
                if v in seen_ids:
                    errs.append("__id__ %d 重复：%r 和 %r"
                                % (v, seen_ids[v], path))
                else:
                    seen_ids[v] = path
            else:
                errs.append("嵌套 dict %r 没有 __id__（客户端 roinit 会 assert）"
                            % (path,))
            for k, v2 in obj.items():
                if k in ("__id__", "__tp__"):
                    continue
                walk_ids(v2, "%s.%s" % (path, k))
        elif isinstance(obj, (list, tuple)):
            for i, v2 in enumerate(obj):
                walk_ids(v2, "%s[%d]" % (path, i))

    for op in ops:
        path = op.path or ()
        if not isinstance(op.property_id, int):
            errs.append("%r 的 property_id 不是整数" % (path,))
        if op.optype == OP_ADD:
            if not isinstance(op.value, dict):
                errs.append("OP_ADD %r 的值不是 dict" % (path,))
            elif not isinstance(op.value.get("__id__"), int):
                errs.append("OP_ADD %r 的值没有 __id__" % (path,))
            if op.property_id <= 0:
                errs.append("OP_ADD %r 的 propertyId=0（客户端找不到父对象）" % (path,))
            # 结构体字段不能 OP_ADD
            if len(path) >= 2:
                ptype = _parent_type_of(ops, path)
                if ptype and path[-1] in _spec.struct_fields(ptype):
                    errs.append("OP_ADD %r 打的是结构体字段（应用 OP_CHANGE）" % (path,))
        if op.optype in (OP_DEL, OP_REMOVE) and op.value is not None:
            errs.append("OP_DEL/REMOVE %r 不该带值" % (path,))
        if op.value is not None:
            walk_ids(op.value, path)
    return errs


def _parent_type_of(ops, path):
    """尽量推断某条 op 的父对象类型（用于结构体字段判定）。

    简化实现：只看路径长度 2 的情况（根容器 -> 键），用根属性声明的 value_type。
    """
    from . import spec as _spec
    if len(path) != 2:
        return None
    sp = _spec.get(path[0])
    return sp.value_type if sp else None


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
            # ★ 根属性值保留 __tp__，嵌套值剥掉（审计 P0-1/P0-2）
            is_root = len(op.path or ()) == 1
            value = _strip_private(op.value, is_root=is_root)
        frames.append(_ms.build_property_sync(
            "", op.property_id, op.optype, op.name, value))
    return frames
