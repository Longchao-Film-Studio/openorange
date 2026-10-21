# -*- coding: utf-8 -*-
"""单一 __id__ 分配器（P0 地基 A 项）。

## 为什么需要它

2026-10-21 四模块审计发现：项目里原本有 **两个互不相知的 __id__ 分配器**：

```
PlayerState.next_property_id()   -> 缓存 property_seq
story._next_id_for_props()       -> 每次扫 props 求 max
```

实测撞号：`1381` 同时被分配给「item_bag 格子」和「done_story_tasks 条目」。
客户端 `_attachToCache` 会打印 `id repeated!` 并**覆盖缓存项**
（`customtype.lua:236-238`），之后所有以该 id 为 propertyId 的同步都会改到
**另一个对象**上；`_detachFromCache` 还会把无辜对象从缓存里删掉。

## 设计

把「下一个可用 id」直接存在 `props` 的私有字段 `_oo_next_id` 里：

- **只依赖 props**，不需要 PlayerState（`story.add_item` 只拿得到 props）
- **跟着存档落盘**，重启不丢
- `_oo_` 前缀，`ms._live_init_dict` 会过滤掉（`ms.py:501-502`），客户端看不到
- 首次分配时取 `max_id(props)`（兼容 avatar_init.json 里 1000+ 的预置 id）

## 规则

**所有创建嵌套对象的地方都必须走 `alloc_id(props)`**：
剧情发奖（story.add_item）、脚手架种数据、以后的任务槽位/街区条目/派遣条目……
禁止任何「扫 props 求 max + 1」的旁路 —— 那正是撞号的来源。
"""
import threading

# 「下一个可用 id」在 props 里的键名。
# 带 _oo_ 前缀，ms._live_init_dict 会过滤，不会下发给客户端。
_OO_NEXT_ID = "_oo_next_id"

_LOCK = threading.RLock()


def max_id(obj, cur=0):
    """递归找 props 里最大的 `__id__`。

    只应在首次分配时调用（之后靠 `_oo_next_id` 缓存）。
    这是全项目**唯一**一份 max 扫描实现 —— 之前有三份
    （`player_state._max_property_id` / `story._max_id` / `scaffold._max_id`）。
    """
    if isinstance(obj, dict):
        v = obj.get("__id__")
        if isinstance(v, int) and v > cur:
            cur = v
        for v2 in obj.values():
            cur = max_id(v2, cur)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            cur = max_id(v, cur)
    return cur


def peek_next_id(props):
    """看当前分配的进度（只读，不分配）。测试和诊断用。"""
    with _LOCK:
        cur = props.get(_OO_NEXT_ID)
        if isinstance(cur, int) and cur > 0:
            return cur
        return max_id(props)


def alloc_id(props):
    """分配一个新的 `__id__`。全项目唯一入口。

    返回的 id 保证：
      - 大于 props 里现有的所有 `__id__`（首次分配时扫一遍）
      - 与之前分配过的都不同（靠 `_oo_next_id` 单调递增）
      - 落盘后重启继续递增（`_oo_next_id` 在 props 里）
    """
    if not isinstance(props, dict):
        raise TypeError("alloc_id 需要 props dict，收到 %r" % (type(props),))
    with _LOCK:
        cur = props.get(_OO_NEXT_ID)
        if not isinstance(cur, int) or cur <= 0:
            # 首次分配：避开 initDict 里已有的 __id__
            cur = max_id(props)
        nid = cur + 1
        props[_OO_NEXT_ID] = nid
        return nid


def verify_unique(props):
    """检查 props 里有没有重复的 `__id__`。返回 (是否唯一, {id: [路径...]})。

    开发期断言用。审计就是靠这个发现 1381 撞号的。
    """
    seen = {}

    def walk(obj, path):
        if isinstance(obj, dict):
            v = obj.get("__id__")
            if isinstance(v, int):
                seen.setdefault(v, []).append(path)
            for k, v2 in obj.items():
                if k == "__id__":
                    continue
                walk(v2, "%s.%s" % (path, k))
        elif isinstance(obj, (list, tuple)):
            for i, v2 in enumerate(obj):
                walk(v2, "%s[%d]" % (path, i))

    walk(props, "")
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    return (not dup), dup
