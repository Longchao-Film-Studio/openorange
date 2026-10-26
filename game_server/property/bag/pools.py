"""随机池（RandomItem）抽取。

## 数据形态

`RandomItem.json` 是两层结构 `{PoolId: {autoId: 条目}}`（735 个池），条目字段：

    itemList   {KeyList:[itemId...], itemId: 数量}   —— 直接给道具
    subPool    {KeyList:[poolId...], poolId: 次数}   —— 嵌套池（实测 619 条引用全部是合法池）
    weight     类型 1 是相对权重（0~2500）；类型 2 是概率（0~1，99.6% ≤1）
    randType   同池一致：1=按权重抽一个；2=逐条独立概率（可能 0~N 个）；0=同 1
    priority   未发现语义差异（全 0 或 3），暂不参与
    maxTimesLimit  同一条目最多被抽中几次（1~3），按日重置

## 语义来源与不确定处

客户端**没有**随机池逻辑（全表只有 tabledata），抽取是官方服务端行为，只能从数据推断：

  · randType=1 权重 0~2500、randType=2 权重 ≤1 —— 两者量纲完全不同，这是
    「相对权重 / 绝对概率」判断的主要依据（见 tests/test_bag_pools.py 的统计断言）
  · maxTimesLimit 1~3 且只出现在少量条目上 —— 按「每天最多抽中 N 次」处理
  · randType=0 只有 1 个池（50021），按 1 处理

P5 实机用盲盒对照验证；若与官方不一致，改这里 + 测试即可（语义集中在一个函数里）。
"""
from dataclasses import dataclass, field
from random import Random
from time import strftime
from typing import Any

from shared.log_tee import get_logger

from ... import gamedata
from . import report
from .errors import BAG_INVALID_ITEM_LIST, BagError

_log = get_logger(__name__)

RNG: Random = Random()
MAX_DEPTH = 6
_LIMIT_KEY = "_oo_draw_limits"


@dataclass
class PoolDraw:
    """一次抽取的结果。"""

    #: 可以入包的道具 (item_id, count)
    items: list[tuple[int, int]] = field(default_factory=list)
    #: 不在 ItemData 的奖励 (id, count)，已落盘，等对应系统上线
    others: list[tuple[int, int]] = field(default_factory=list)
    #: 本层抽中的条目 autoId（不含嵌套池）
    top_auto_ids: list[int] = field(default_factory=list)
    #: 全部抽中的条目 autoId（含嵌套池），便于排障
    auto_ids: list[int] = field(default_factory=list)


def _today() -> str:
    return strftime("%Y%m%d")


def _limits(props: dict[str, Any]) -> dict[str, Any]:
    d = props.get(_LIMIT_KEY)
    if not isinstance(d, dict):
        d = {}
        props[_LIMIT_KEY] = d
    return d


def _limit_ok(props: dict[str, Any] | None, auto_id: int, limit: int | None) -> bool:
    """maxTimesLimit 检查（按日重置）。"""
    if props is None or not limit or limit <= 0:
        return True
    rec = _limits(props).get(str(auto_id))
    today = _today()
    if not isinstance(rec, dict) or rec.get("d") != today:
        _limits(props)[str(auto_id)] = {"d": today, "n": 0}
        return True
    return int(rec.get("n") or 0) < int(limit)


def _hit(props: dict[str, Any] | None, auto_id: int) -> None:
    if props is None:
        return
    rec = _limits(props).get(str(auto_id))
    if isinstance(rec, dict):
        rec["n"] = int(rec.get("n") or 0) + 1


def _weight(e: dict[str, Any]) -> float:
    try:
        return float(e.get("weight") or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_entry(props: dict[str, Any] | None, entry: dict[str, Any], out: PoolDraw,
                   depth: int) -> None:
    """把一条条目解析成奖励（道具 / 其它 / 嵌套池）。"""
    if depth > MAX_DEPTH:
        _log.error("[pool] 嵌套超过 %s 层，停止（autoId=%s）", MAX_DEPTH, entry.get("autoId"))
        return
    item_list = entry.get("itemList")
    if isinstance(item_list, dict):
        for k in item_list.get("KeyList") or []:
            try:
                iid = int(k)
                cnt = int(item_list.get(str(k)) or item_list.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if cnt <= 0:
                continue
            if isinstance(gamedata.item(iid), dict):
                out.items.append((iid, cnt))
            else:
                # 装备/时装/商品等非道具奖励：背包造不出来，落盘等对应系统
                report.note("pool_reward", iid, "不在 ItemData（装备/时装/商品等）")
                out.others.append((iid, cnt))
    sub = entry.get("subPool")
    if isinstance(sub, dict):
        for k in sub.get("KeyList") or []:
            try:
                sub_id = int(k)
                times = int(sub.get(str(k)) or sub.get(k) or 1)
            except (TypeError, ValueError):
                continue
            nested = draw(props, sub_id, max(1, times), depth=depth + 1)
            out.items.extend(nested.items)
            out.others.extend(nested.others)
            out.auto_ids.extend(nested.auto_ids)


def draw(props: dict[str, Any] | None, pool_id: int, times: int = 1, *,
         depth: int = 0, rng: Random | None = None) -> PoolDraw:
    """从池子里抽 times 次。池不存在抛 Bag_InvaildItemList（调用方不扣道具）。"""
    pool = gamedata.random_item_pool(pool_id)
    if not pool:
        raise BagError(BAG_INVALID_ITEM_LIST, f"随机池 {pool_id} 不存在")
    rand = rng or RNG
    out = PoolDraw()
    entries = sorted(pool.items())
    rand_type = 1
    for _auto, e in entries[:1]:
        try:
            rand_type = int(e.get("randType") or 1)
        except (TypeError, ValueError):
            rand_type = 1

    for _ in range(max(1, int(times))):
        if rand_type == 2:
            # 逐条独立概率
            for auto_id, e in entries:
                limit = e.get("maxTimesLimit")
                if not _limit_ok(props, auto_id, limit):
                    continue
                w = _weight(e)
                if w <= 0:
                    continue
                if rand.random() < min(1.0, w):
                    _hit(props, auto_id)
                    out.top_auto_ids.append(auto_id)
                    out.auto_ids.append(auto_id)
                    _resolve_entry(props, e, out, depth)
        else:
            # 相对权重抽一个
            cands = [(a, e) for a, e in entries
                     if _weight(e) > 0 and _limit_ok(props, a, e.get("maxTimesLimit"))]
            total = sum(_weight(e) for _a, e in cands)
            if total <= 0:
                continue
            roll = rand.random() * total
            acc = 0.0
            for auto_id, e in cands:
                acc += _weight(e)
                if roll <= acc:
                    _hit(props, auto_id)
                    out.top_auto_ids.append(auto_id)
                    out.auto_ids.append(auto_id)
                    _resolve_entry(props, e, out, depth)
                    break
    return out
