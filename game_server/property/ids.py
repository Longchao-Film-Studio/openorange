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
- `_oo_` 前缀，`ms.live_init_dict` 会过滤掉（`ms.py:501-502`），客户端看不到
- 首次分配时取 `max_id(props)`（兼容 avatar_init.json 里 1000+ 的预置 id）

## 规则

**所有创建嵌套对象的地方都必须走 `alloc_id(props)`**：
剧情发奖（story.add_item）、脚手架种数据、以后的任务槽位/街区条目/派遣条目……
禁止任何「扫 props 求 max + 1」的旁路 —— 那正是撞号的来源。
"""
import threading

from shared.log_tee import get_logger

_log = get_logger(__name__)

# 「下一个可用 id」在 props 里的键名。
# 带 _oo_ 前缀，ms.live_init_dict 会过滤，不会下发给客户端。
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
    """看**最后已分配**的 id（只读，不分配）。

    注意语义：返回的是「已经用掉的最后一个」，不是「下一个可用」。
    下一次 alloc_id 会返回 peek_next_id + 1。
    """
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
        raise TypeError(f"alloc_id 需要 props dict，收到 {type(props)!r}")
    with _LOCK:
        cur = props.get(_OO_NEXT_ID)
        if not isinstance(cur, int) or cur <= 0:
            # 首次分配：避开 initDict 里已有的 __id__
            cur = max_id(props)
        nid = cur + 1
        props[_OO_NEXT_ID] = nid
        return nid


def reconcile(props, warn=True):
    """加载存档时校正分配计数器 + 检测已有的撞号。

    ★ 为什么必须有这一步（2026-10-21 审计 P0-2/P0-3）：
      alloc_id 只在 _oo_next_id 缺失时扫 max，之后就信任它。
      但外部写入（GM 工具、客户端异常值、还没迁移的老代码）会往 props 里
      塞更大的 __id__ 而不更新计数器 —— 下一次分配就会撞。
      实测：counter=100 但 props 里已有 __id__=500 时，分配会返回 500。

    所以每次读档都做一次 O(n) 校正：把 counter 抬到真实 max。
    代价是每次登录一次全量扫描（218 属性，毫秒级），可以接受。

    返回 (是否校正过, 重复 id 字典)。
    """
    fixed = False
    real_max = max_id(props)
    cur = props.get(_OO_NEXT_ID)
    if not isinstance(cur, int) or cur < real_max:
        props[_OO_NEXT_ID] = real_max
        fixed = True
    ok, dup = verify_unique(props)
    if warn and not ok:
        _log.info("[ids] ⚠ 存档里有重复 __id__：{}".format(", ".join(f"{k}:{v}" for k, v in list(dup.items())[:5])))
        _log.info("[ids]   客户端会打印 id repeated! 并覆盖缓存项 —— 建议用"
              " tools/fix_dup_ids.py 修一次")
    return fixed, dup


def verify_unique(props):
    """检查 props 里有没有重复的 `__id__`。返回 (是否唯一, {id: [路径...]})。

    开发期断言用。审计就是靠这个发现 1381 撞号的。
    """
    seen = {}

    def walk(obj, path):
        if isinstance(obj, dict):
            v = obj.get("__id__")
            # bool 是 int 的子类，要排除（否则 True 和 1 会被当成重复）
            if isinstance(v, int) and not isinstance(v, bool):
                seen.setdefault(v, []).append(path)
            for k, v2 in obj.items():
                if k == "__id__":
                    continue
                walk(v2, f"{path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v2 in enumerate(obj):
                walk(v2, f'{path}[{i}]')

    walk(props, "")
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    return (not dup), dup
