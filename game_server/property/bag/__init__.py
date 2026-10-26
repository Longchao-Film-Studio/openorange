"""背包与道具效果（三层架构的公开入口）。

    item_use.apply()    使用道具（唯一入口：解析 ext / 校验 / 原子执行 / 错误码 / 帧）
    grants.grant()      发放道具（唯一入口：堆叠 / maxNum / notStack / 钥匙 / ext / 过期 / autoUse）
    effects.EFFECTS     useType -> handler 注册表（未登记 = 显式 UNSUPPORTED）

详细设计见 docs/方案-背包与道具效果.md。
"""
from . import effects, grants, item_use, keys
from .errors import BagError

__all__ = ["BagError", "effects", "grants", "item_use", "keys"]
