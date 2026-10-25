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
from shared.log_tee import get_logger

from .tx import OP_ADD, OP_DEL, OP_INSERT, OP_REMOVE

_log = get_logger(__name__)

# 属性同步帧里 ownerid 的取值（客户端 core/client/gateclientrpchandler.lua:269-288）：
#   空串 = 「就是收帧的这个玩家自己」；非空 = 别人的 uid（客户端会 getRealOwner）。
# 私服一连接一账号，服务端推的永远是自己，所以这里只有 SYNC_SELF。
SYNC_SELF = ""


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
                    errs.append(f'__id__ {v:d} 重复：{seen_ids[v]!r} 和 {path!r}')
                else:
                    seen_ids[v] = path
            else:
                errs.append(f"嵌套 dict {path!r} 没有 __id__（客户端 roinit 会 assert）")
            for k, v2 in obj.items():
                if k in ("__id__", "__tp__"):
                    continue
                walk_ids(v2, f"{path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v2 in enumerate(obj):
                walk_ids(v2, f'{path}[{i}]')

    for op in ops:
        path = op.path or ()
        if not isinstance(op.property_id, int):
            errs.append(f"{path!r} 的 property_id 不是整数")
        if op.optype == OP_ADD:
            # ★ 容器元素可能是「自定义类型」也可能是「简单类型」：
            #   item_bag / street_info 这类元素是结构体 -> 值必须是带 __id__ 的表；
            #   battle_teach_state（IntDictKeyInt）这类元素就是 int -> 值就是标量。
            #   以前一刀切要求「必须带 __id__」，把 battle_teach_state 的 OP_ADD
            #   误报成错误（生产路径 strict=False 所以只刷警告，但语义是错的）。
            # 注意 _parent_type_of 返回的已经是**元素类型**（sp.value_type），
            # 不要再 value_type() 一次 —— 双重解引用会拿到错的类型。
            vtype = _parent_type_of(ops, path) if len(path) >= 2 else None
            elem_custom = bool(vtype) and (_spec.is_struct(vtype)
                                          or _spec.is_container(vtype))
            if isinstance(op.value, dict):
                if not isinstance(op.value.get("__id__"), int):
                    errs.append(f"OP_ADD {path!r} 的值没有 __id__")
                if vtype is not None and not elem_custom:
                    errs.append(f"OP_ADD {path!r} 的元素类型 {vtype!r} 是简单类型，值不该是表")
            elif vtype is None or elem_custom:
                # 父类型查不到（自定义根属性）时按旧规则从严：值必须是带 __id__ 的表
                errs.append(f"OP_ADD {path!r} 的值不是 dict")
            if op.property_id <= 0:
                errs.append(f"OP_ADD {path!r} 的 propertyId=0（客户端找不到父对象）")
            # 结构体字段不能 OP_ADD
            if len(path) >= 2:
                ptype = _parent_type_of(ops, path)
                if ptype and path[-1] in _spec.struct_fields(ptype):
                    errs.append(f"OP_ADD {path!r} 打的是结构体字段（应用 OP_CHANGE）")
        if op.optype in (OP_DEL, OP_REMOVE) and op.value is not None:
            errs.append(f"OP_DEL/REMOVE {path!r} 不该带值")
        # ★ B 段遗留：OP_INSERT / OP_REMOVE 以前没有任何校验（也没人产出）。
        #   客户端 __OpInsert__/__OpRemove__ 的第一句都是 assert(customObj ~= nil)，
        #   而且 name 会无条件 tonumber —— propertyId 填 0 或 name 不是正整数都会崩。
        if op.optype in (OP_INSERT, OP_REMOVE):
            if not isinstance(op.property_id, int) or op.property_id <= 0:
                errs.append(f"OP_INSERT/REMOVE {path!r} 的 propertyId 必须是父 list 的 __id__（>0）")
            try:
                pos = int(op.name)
            except Exception:
                pos = None
            if pos is None or pos < 1:
                errs.append(f"OP_INSERT/REMOVE {path!r} 的 name={op.name!r} 必须是 1-based 位置")
        if op.optype == OP_INSERT and op.value is None:
            errs.append(f"OP_INSERT {path!r} 没有值")
        if op.value is not None:
            walk_ids(op.value, path)
    return errs


def _parent_type_of(_ops, path):
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
            raise ValueError("变更集校验失败：{}".format("; ".join(errs)))
        for e in errs:
            _log.debug(f"[tx] ⚠ 变更集校验: {e}")

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
            SYNC_SELF, op.property_id, op.optype, op.name, value))
    return frames
