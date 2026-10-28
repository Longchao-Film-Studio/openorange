"""格子钥匙规范（背包的唯一键分配器）。

## 规范

钥匙 = `"g" + 单调序号`，序号存 `props["_oo_next_grid"]`，**永不回收**。

- 不用 `max+1`：删除格子后序号会被复用（原 story.add_item 就是这么写的），
  而客户端把 `ItemInfo.id` 当 uuid 用（`avatarbagcomp.lua:287-296`），复用会让
  唯一实例（装备/圣遗物/时装）串号。
- 不用 uuid4：排序丢失、调试困难、无收益。
- 旧存档迁移：把 `"1"` 这类旧键改成 `"g1"`，同步更新 `grid["id"]`；登录走全量快照，
  没有增量风险。

## 为什么单独一个模块

钥匙是「背包结构」的公共约定：发放、使用、删除、迁移都依赖它。
散在各处就会出现「有的地方 new_key = max+1、有的地方 str(n)」——原代码就是这样。
"""
from typing import Any

from ...write_guard import write_guard

_NEXT_KEY = "_oo_next_grid"
PREFIX = "g"


def _as_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.removeprefix("#")
        s = s.removeprefix(PREFIX)
        if s.isdigit():
            return int(s)
    return None


def _infer_next(props: dict[str, Any]) -> int:
    """首次分配时推断下一个序号（兼容旧存档 + 手改数据）。"""
    best = 0
    bag = props.get("item_bag")
    if isinstance(bag, dict):
        for key in bag:
            n = _as_int(key)
            if n is not None and n > best:
                best = n
    cur = _as_int(props.get(_NEXT_KEY))
    if cur is not None and cur > best:
        best = cur
    return best


def next_seq(props: dict[str, Any]) -> int:
    """取下一个序号（不分配）。"""
    cur = _as_int(props.get(_NEXT_KEY))
    if cur is None or cur <= 0:
        cur = _infer_next(props)
    return cur + 1


def alloc(props: dict[str, Any]) -> str:
    #属性写入口，在这里开守卫（格子键分配）。
    with write_guard("alloc"):
        """分配一个新钥匙（写 `_oo_next_grid`，保证不回收）。"""
        n = next_seq(props)
        props[_NEXT_KEY] = n
        return f"{PREFIX}{n}"


def is_canonical(key: object) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX) and key[len(PREFIX):].isdigit()


def migrate(props: dict[str, Any]) -> int:
    #属性写入口，在这里开守卫（格子键迁移）。
    with write_guard("migrate"):
        """把 item_bag 的旧键（"1"/#1/整数）改成规范键，返回迁移数量。

        幂等：已经规范化的键不动。迁移时重建 dict 以保持顺序（客户端按插入顺序展示）。
        """
        bag = props.get("item_bag")
        if not isinstance(bag, dict):
            return 0
        meta: dict[Any, Any] = {}
        entries: list[tuple[Any, Any]] = []
        for k, v in bag.items():
            if isinstance(k, str) and k.startswith("__"):
                meta[k] = v
            else:
                entries.append((k, v))
        changed = 0
        new_bag: dict[Any, Any] = {}
        for k, v in entries:
            if is_canonical(k):
                new_bag[k] = v
                continue
            key = alloc(props)
            new_bag[key] = v
            if isinstance(v, dict):
                v["id"] = key
            changed += 1
        if not changed:
            return 0
        new_bag.update(meta)
        props["item_bag"] = new_bag
        return changed
