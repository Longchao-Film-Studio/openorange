"""过期清理：道具格子 + 装饰（Fashion）按 expiry 时间下架。

## 契约（客户端侧）

  · ItemInfo.expireTime   秒级时间戳；0 = 永久（grants 按 ItemData.validTime 写入）
  · Fashion.expiration_time 同上（userdataprop.lua:25-39 add() 里算）
  · 过期后客户端**不会自己删**（只定时器清理），服务端必须自己下架：
      - 道具：从 item_bag 删格子；最近过期的记进 _oo_expired_items 供
        QueryExpiredItems_CS 返回（storepanel.lua:83-96 读 response.expired_items，
        元素形如 {itemId, num}）
      - 装饰：从 *_bag 删除，按 FashionType 记进 expired_fashions[type][id]=now
        （userdataprop.lua:88-93 的 processExpiration 就是这么写的；
         node_pop_playerinformationgroup.lua:77-96 读 response.expired_fashions）

## 调用时机

登录（ms/initdict._stage_boot）：在构建 initDict 之前扫一遍，
过期格子不会出现在快照里 —— 不需要额外删帧。
"""
from dataclasses import dataclass, field
from time import time
from typing import Any

from shared.log_tee import get_logger

from ...write_guard import write_guard
from .. import factory

_log = get_logger(__name__)

#: FashionBag -> FashionType（gamecommon/customtypes/userdata/userdataprop.lua:5-14）
FASHION_BAGS: dict[str, int] = {
    "portrait_bag": 1,          # portrait
    "player_title_bag": 2,      # player_title
    "portrait_frame_bag": 3,    # portrait_frame
    "emoji_bag": 4,             # emoj
    "background_bag": 5,        # background
    "player_card_bag": 6,       # card
    "player_card_frame_bag": 7,  # card_frame
    "chat_bubble_bag": 8,       # chat_bubble
}

EXPIRED_ITEMS_KEY = "_oo_expired_items"
MAX_EXPIRED_RECORDS = 100


@dataclass
class SweepResult:
    """一次清理的结果（登录时只关心非空即保存）。"""

    items: list[dict[str, int]] = field(default_factory=list)      # [{itemId, num}]
    fashion_ids: dict[int, list[int]] = field(default_factory=dict)  # type -> [id]
    removed_grids: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.items or self.fashion_ids)


def _now(now: int | None) -> float:
    return float(now if now is not None else int(time()))


def _expired(value: Any, now: float) -> bool:  # noqa: ANN401 — 存档字段类型不可信（JSON 可能是 str/int/None）
    try:
        t = float(value or 0)
    except (TypeError, ValueError):
        return False
    return 0 < t <= now


def sweep(props: dict[str, Any], now: int | None = None) -> SweepResult:
    #属性写入口，在这里开守卫（过期清扫）。
    with write_guard("sweep"):
        """清理过期道具与装饰（直接改 props）。返回清理结果。"""
        ts = _now(now)
        res = SweepResult()

        bag = props.get("item_bag")
        if isinstance(bag, dict):
            for key in [k for k in bag if not str(k).startswith("__")]:
                grid = bag.get(key)
                if not isinstance(grid, dict):
                    continue
                if not _expired(grid.get("expireTime"), ts):
                    continue
                rec = {"itemId": int(grid.get("itemId") or 0),
                       "num": int(grid.get("num") or 0)}
                del bag[key]
                res.items.append(rec)
                res.removed_grids.append(str(key))
            if res.items:
                hist = props.get(EXPIRED_ITEMS_KEY)
                if not isinstance(hist, list):
                    hist = []
                hist.extend(res.items)
                props[EXPIRED_ITEMS_KEY] = hist[-MAX_EXPIRED_RECORDS:]

        for bag_name, ftype in FASHION_BAGS.items():
            fbag = props.get(bag_name)
            if not isinstance(fbag, dict):
                continue
            for key in [k for k in fbag if not str(k).startswith("__")]:
                entry = fbag.get(key)
                if not isinstance(entry, dict):
                    continue
                if not _expired(entry.get("expiration_time"), ts):
                    continue
                del fbag[key]
                res.fashion_ids.setdefault(ftype, []).append(int(key))
                # 记录到 expired_fashions[type][id] = now（客户端读这个展示"已过期"）
                #
                # ★ 2026-10-27 实机踩坑：expired_fashions 的值类型是 IntDictKeyInt
                #   —— 它是**自定义字典**，客户端 roinit 会 assert(__id__ ~= nil)。
                #   之前这里直接塞了裸 dict，登录快照缺 __id__ -> 客户端 ClientAvatar:init 崩。
                #   所以内层必须用 factory.new_instance 建（带 __tp__/__id__）。
                ef = props.get("expired_fashions")
                if not isinstance(ef, dict):
                    ef = factory.new_instance(props, "IntDictDictKeyInt")
                    props["expired_fashions"] = ef
                slot = ef.get(ftype)
                if not isinstance(slot, dict) or not slot.get("__id__"):
                    old = dict(slot) if isinstance(slot, dict) else {}
                    old.pop("__tp__", None)
                    old.pop("__id__", None)
                    slot = factory.new_instance(props, "IntDictKeyInt")
                    for k2, v2 in old.items():
                        slot[k2] = v2
                    ef[ftype] = slot
                slot[int(key)] = int(ts)

        if res:
            _log.info("[bag] 过期清理：道具 %d 个、装饰 %d 个",
                      len(res.items), sum(len(v) for v in res.fashion_ids.values()))
        return res


def expired_items(props: dict[str, Any]) -> list[dict[str, int]]:
    """最近过期的道具（QueryExpiredItems_CS 用，客户端读 response.expired_items）。"""
    hist = props.get(EXPIRED_ITEMS_KEY)
    return [dict(x) for x in hist] if isinstance(hist, list) else []


def expired_fashions(props: dict[str, Any]) -> dict[int, dict[int, int]]:
    """已过期装饰 {FashionType: {id: 时间}}（QueryExpiredFashions 用）。"""
    ef = props.get("expired_fashions")
    out: dict[int, dict[int, int]] = {}
    if not isinstance(ef, dict):
        return out
    for k, v in ef.items():
        if str(k).startswith("__") or not isinstance(v, dict):
            continue
        try:
            t = int(k)
        except (TypeError, ValueError):
            continue
        out[t] = {int(i): int(w) for i, w in v.items()
                  if not str(i).startswith("__") and isinstance(w, (int, float))}
    return out


def reset_for_tests() -> None:
    return None
