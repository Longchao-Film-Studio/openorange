"""道具效果注册表（useType -> handler）。

## 设计

handler **只描述「要发生什么」**，不碰 props：
    返回 list[Op]，由 item_use 统一提交（Tx）。
好处：
  · 原子性天然成立 —— 先算完 ops 再统一写，任何一步失败都不落盘
  · 可测试 —— 断言 ops 即可，不需要真存档
  · 可扩展 —— 新增 useType 只需注册一个 handler + 一条数据驱动测试

## 未实现

未登记的 useType 走 UNSUPPORTED：item_use 返回 Bag_SystemNotUnlocked 且**不扣道具**，
并落盘 work/logs/unknown_item_effects.tsv（P5 落地）。
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..tx import Tx


@dataclass
class PropOp:
    """改一个简单属性（Exp / Crystal / 各类计数）。"""

    name: str
    value: int | float


@dataclass
class GrantOp:
    """发放道具（走 grants.grant）。"""

    item_id: int
    count: int


@dataclass
class BagAddOp:
    """往一个 FashionBag 里加装饰（id/expiration_time）。"""

    bag: str
    key: int
    value: dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomOp:
    """自定义效果：直接改 props / 走其它领域模块，并返回自己的同步帧。

    用在「目标不在背包里」的效果上（街头声望在 street_info、好感在 favor_exp_map），
    fn 拿 props 干活并返回 frames；item_use 会把这些帧插在应答之前。
    """

    fn: Callable[[dict[str, Any]], list[bytes]]


Op = PropOp | GrantOp | BagAddOp | CustomOp

#: useType -> handler(props, cfg, count, ext) -> list[Op]
EFFECTS: dict[str, Callable[..., list[Op]]] = {}

#: 明确「暂不支持」的 useType（依赖未实现的系统）。显式列出，便于数据驱动测试。
#: 计划内、但还没实现的效果（P2/P4 落地）。命中时同样返回 15009 + 不扣道具，
#: 但语义是「已排期」而不是「依赖未实现系统」—— 数据驱动测试据此区分。
PENDING_USE_TYPES: dict[str, str] = {
}

UNSUPPORTED_USE_TYPES: dict[str, str] = {
    # ★ 2026-10-27：VIP/通行证解锁类。它们在官方是**商店购买 -> 服务端发货**的流程，
    #   道具本身没有可执行效果（数据里只有 vip 等级 / activityId 这类支付参数）。
    #   背包只做"发到手"，解锁逻辑属于商店/支付系统（未实现）。
    "battlepass": "商店购买的通行证 VIP（vip 2/3），解锁走支付/发货流程",
    "amassbattlepass_1": "活动通行证 VIP（activityId + Amassvip），解锁走活动支付流程",
    "roguebattlepass": "肉鸽通行证 VIP（roguevip），解锁走肉鸽系统/支付流程",
    "readybattlepass_1": "预创建通行证 VIP 1（readyvip_1），解锁走对应系统",
    "readybattlepass_2": "预创建通行证 VIP 2（readyvip_2），解锁走对应系统",
    "backbattlepass": "回归通行证 VIP（backvip），解锁走回归活动系统",
    "char": "角色发放链路未实现",
    "charSkin": "皮肤系统未实现",
    "selectChar": "自选角色（依赖角色系统）",
    "charAttr": "角色经验/等级未实现",
    "pupil": "徒弟系统未实现",
    "pet": "宠物系统未实现",
    "SigilAttr": "圣遗物系统未实现",
    "FashionEquipmentExp": "时装装备实例经验未实现",
    "monthlypass": "月卡/付费链路未实现",
}


def register(use_type: str) -> Callable[[Callable[..., list[Op]]], Callable[..., list[Op]]]:
    def deco(fn: Callable[..., list[Op]]) -> Callable[..., list[Op]]:
        EFFECTS[use_type] = fn
        return fn
    return deco


def supports(use_type: str) -> bool:
    return use_type in EFFECTS


def handler_for(use_type: str) -> Callable[..., list[Op]] | None:
    return EFFECTS.get(use_type)


def apply_auto_use(props: dict[str, Any], item_id: int, count: int, *,
                   tx: "Tx", source: str = "", depth: int = 0) -> list[Op] | None:
    """autoUse 的实现入口：算出 ops 并提交。返回 ops；不支持则 None。"""
    from ... import gamedata
    use_type = gamedata.item_use_type(item_id)
    handler = handler_for(use_type)
    if handler is None:
        return None
    ops = handler(props, gamedata.item(item_id) or {}, count, {})
    # 延迟导入避免循环（item_use 会 import effects）
    from . import item_use
    item_use.materialize(props, ops, tx=tx, source=source, depth=depth)
    return ops
