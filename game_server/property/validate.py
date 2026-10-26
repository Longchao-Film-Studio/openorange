#!/usr/bin/env python3
"""属性树结构校验：自定义类型节点必须带 `__id__`。

## 为什么需要它（2026-10-26 两次「整号登不上」）

客户端 `CustomDict:roinit` / `CustomList:roinit` 是无条件断言的：
    local id = value.__id__;  assert(id ~= nil)
没有任何例外——空 dict、混合类型、嵌套子对象，全都要 id。

而服务端 `Tx._ensure_id` 的契约边界很窄：**只给直接传给 set() 的那个对象补 id**，
不递归子对象。于是「一次 set 塞多层新对象」就会漏掉内层的 id：
    · 短信：address_list[chat][topic] 的 NpcChatInfo、group_npc_ids、topic_infos
    · 万一背包/邮件/街景也这么写，同样会漏
落盘后客户端每次登录都在 ClientAvatar:init 崩 -> 回登录界面。

## 判定规则（唯一一条）
按客户端声明（property/spec.py，来自 generated/config.lua）走整棵树：
  · 简单类型（int/double/string/boolean）       -> 不需要 id，跳过
  · 自定义类型（struct / 容器 / 混合）            -> 必须有 __id__
  · 结构体字段按 struct_fields 继续往下走
  · 容器元素按 value_type 继续往下走
  · 混合类型（既有字段又是字典，如 StreetInfoDict）：自身字段先按字段走，
    剩下的键才当容器元素 —— 否则会把 all_prestige_exp 当成一个元素误报

## 两个落点
  1) 读档：ids.reconcile() 里调用 find_missing_ids() —— 抓「已经坏掉的数据」
  2) 写入：Tx.set()/insert()/append() 里调用 check_value() —— 抓「代码写坏的数据」
第二个才是根治：测试里当场红，不用等玩家登录。
"""
from . import spec

# 元字段：不是自定义对象，是协议里的标识/类型标记
META_KEYS = ("__id__", "__tp__")


def _fmt(path):
    """把路径元组渲染成人能读的字符串：prop.a[123].b"""
    out = ""
    for seg in path:
        if isinstance(seg, int) or (isinstance(seg, str) and seg.startswith("#")):
            out += "[" + str(seg) + "]"
        else:
            out += ("." if out else "") + str(seg)
    return out


def check_value(value, type_name, path, missing):
    """检查一个值（及其子树）是否满足 id 契约，把问题追加进 missing。

    missing: list[(path_str, type_name)]，由调用方传入以便汇总。
    """
    if type_name is None or spec.is_simple(type_name):
        return
    c = spec.custom(type_name)
    if c is None:
        return                      # 声明里没有的类型：查不了，不误报
    node_ok = isinstance(value, dict) and isinstance(value.get("__id__"), int)
    if not node_ok:
        missing.append((_fmt(path), type_name))
        if not isinstance(value, dict):
            return                  # 连 dict 都不是：结构已坏，不再往下挖
    fields = (c.get("props") or {})
    for fname, fdecl in fields.items():
        if fname in value:
            ftype = fdecl[0] if isinstance(fdecl, (list, tuple)) else fdecl
            check_value(value[fname], ftype, path + (fname,), missing)
    vtype = c.get("value_type")
    if vtype:
        for k, v in value.items():
            if k in META_KEYS or k in fields:
                continue            # 元字段 / 混合类型的自身字段
            if str(k).startswith("_oo_"):
                continue            # 服务端私有字段（sync 层会剔除，不下发）
            check_value(v, vtype, path + (k,), missing)


def find_missing_ids(props, limit=0):
    """扫整个 props，返回缺 __id__ 的自定义类型节点 [(path_str, type_name)]。

    limit > 0 时最多返回这么多条（日志用）。
    """
    missing = []
    if not isinstance(props, dict):
        return missing
    for name, value in props.items():
        if name in META_KEYS or str(name).startswith("_oo_"):
            continue                # 内部记账键（_oo_next_id 等）不在声明里
        sp = spec.get(name)
        if sp is None:
            continue                # 声明里没有的根属性：不误报
        check_value(value, sp.type, (name,), missing)
        if limit and len(missing) >= limit:
            break
    return missing[:limit] if limit else missing


def invariant_holds(props):
    """不变量：整棵属性树没有缺 id 的自定义节点。"""
    return not find_missing_ids(props)
