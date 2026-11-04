"""背包之外的「特殊目标」效果（声望 / 好感）。

这两个效果的落点不在背包里，但入口仍然是道具效果：
  · 街头声望：street_info[streetId].prestige_exp（等级/称号由 street.prestige 算）
  · 角色好感：favor_exp_map[charId] / popularity_map[charId]
    （等级不落库，由 favor_level() 从经验现算 —— 与客户端同一套算法）

handler 只返回 CustomOp，真正的写入在这里，帧由各自领域模块生成。
"""
from types import SimpleNamespace
from typing import Any, cast

from shared.log_tee import get_logger

from . import report

_log = get_logger(__name__)


def _shim(props: dict[str, Any]) -> SimpleNamespace:
    """street.prestige 的函数签名要 st（st.props / st.set / st.save）——给个最小壳。"""
    def _set(name: str, value: object) -> None:
        props[name] = value

    return SimpleNamespace(props=props, set=_set, save=lambda: None)


def add_prestige_exp(props: dict[str, Any], street_id: int, exp: int) -> list[bytes]:
    """加街头声望经验，返回同步帧（走 street.prestige 的唯一入口）。"""
    from ...street import prestige

    st = _shim(props)
    si, entry, sid = prestige.prestige_info_of(st, street_id)[:3]
    if entry is None:
        report.note("prestige", street_id, "street_info 里没有该街区")
        _log.warning("[bag] 街头 %s 不在 street_info，声望道具未生效", street_id)
        return []
    # prestige 模块还没类型注解（旧代码），这里显式收窄返回类型
    return list(cast("list[bytes]", prestige.apply_prestige_exp(st, si, entry, sid, exp, save=False)))


def favor_level(props: dict[str, Any], char_id: int) -> int:
    """某个角色的好感度等级（纯读，不写 props）。

    ★ 必须和客户端**逐行一致**，否则任务的「好感度达到 N 级」和玩家看到的对不上。
      客户端 entities/avatarcomponent/avatarfavorspacecomp.lua:346-372:

        local curLevel = 1
        for _, v in ipairs(config) do
          curLevel = v.FavorLevel
          if totalExp < v.CostFavorExp then break
          elseif curLevel == maxFavorLevel - 1 then return curLevel + 1 end
        end
        return curLevel

    ★ 数据形状（BDD FavorLevelData，27 个角色）：
        FavorLevelData[charId] = [{FavorLevel, CostFavorExp, CharId, Id}, ...]
      CostFavorExp 是「达到该等级所需的**累计**经验」，不是增量。

    ★ 等级不落库：props 里只有 favor_exp_map[charId]，等级是算出来的。
      这和客户端一致（客户端也从 favor_exp_map 现算），
      所以服务端不需要为等级新增根属性。
    """
    from ... import gamedata

    try:
        char_id = int(char_id)
    except (TypeError, ValueError):
        return 1
    table = gamedata.table("FavorLevelData") or {}
    cfg = table.get(str(char_id)) or table.get(char_id)
    if not isinstance(cfg, list) or not cfg:
        return 1

    fm = props.get("favor_exp_map")
    total = 0
    if isinstance(fm, dict):
        raw = fm.get(char_id)
        try:
            total = int(raw or 0)
        except (TypeError, ValueError):
            total = 0

    # GetMaxFavorLevel = max(FavorLevel) + 1
    max_level = 0
    for row in cfg:
        if not isinstance(row, dict):
            continue
        try:
            max_level = max(max_level, int(row.get("FavorLevel") or 0))
        except (TypeError, ValueError):
            continue
    max_level += 1

    cur = 1
    for row in cfg:
        if not isinstance(row, dict):
            continue
        try:
            lv = int(row.get("FavorLevel") or 0)
            need = int(row.get("CostFavorExp") or 0)
        except (TypeError, ValueError):
            continue
        if lv <= 0:
            continue
        cur = lv
        if total < need:
            break
        if cur == max_level - 1:
            return cur + 1
    return cur


def add_char_favor(props: dict[str, Any], char_id: int, exp: int, popularity: int) -> list[bytes]:
    """加角色好感/人气，返回同步帧（favor_exp_map / popularity_map 的子属性同步）。"""
    from ..tx import Tx

    tx = Tx(props)
    for root, add in (("favor_exp_map", exp), ("popularity_map", popularity)):
        if add == 0:
            continue
        m = props.get(root)
        if not isinstance(m, dict):
            report.note("favor", root, "属性不存在")
            continue
        cur = m.get(char_id)
        base = int(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else 0
        tx.set((root, char_id), base + int(add))
    frames: list[bytes] = list(tx.frames())
    return frames
