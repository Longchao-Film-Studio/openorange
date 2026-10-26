"""背包之外的「特殊目标」效果（声望 / 好感）。

这两个效果的落点不在背包里，但入口仍然是道具效果：
  · 街头声望：street_info[streetId].prestige_exp（等级/称号由 street.prestige 算）
  · 角色好感：favor_exp_map[charId] / popularity_map[charId]

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
    si, entry, sid, is_new = prestige.prestige_info_of(st, street_id)
    if entry is None:
        report.note("prestige", street_id, "street_info 里没有该街区")
        _log.warning("[bag] 街头 %s 不在 street_info，声望道具未生效", street_id)
        return []
    # prestige 模块还没类型注解（旧代码），这里显式收窄返回类型
    return list(cast("list[bytes]", prestige.apply_prestige_exp(st, si, entry, sid, exp, save=False, is_new=is_new)))


def add_char_favor(props: dict[str, Any], char_id: int, exp: int, popularity: int) -> list[bytes]:
    """加角色好感/人气，返回同步帧（favor_exp_map / popularity_map 的子属性同步）。"""
    from ... import ms as msmod
    from ...rpc_common import SYNC_SELF, sync_frame

    frames: list[bytes] = []
    for root, add in (("favor_exp_map", exp), ("popularity_map", popularity)):
        if add == 0:
            continue
        m = props.get(root)
        if not isinstance(m, dict):
            report.note("favor", root, "属性不存在")
            continue
        cur = m.get(char_id)
        base = int(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else 0
        m[char_id] = base + int(add)
        frames.append(sync_frame(SYNC_SELF, m.get("__id__") or 0,
                                 msmod.OP_CHANGE, str(char_id), m[char_id]))
    return frames
