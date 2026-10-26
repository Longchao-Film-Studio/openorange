"""奖励展示（Avatar_Reward_SC / Avatar_Reward_Source_SC）。

客户端契约（avatarbagcomp.lua:510-551）：参数是一张表 {itemId: {num, gridids}}；
  有 gridids 时按实例展示（唯一实例/装备），没有就按普通道具展示。
config 声明见 generated/config.lua:7829-7830。

用途：开盲盒 / 用礼包 / 领取奖励后，让客户端弹"获得物品"面板。
属性同步只刷新背包，不会弹面板 —— 这就是"用了盲盒没反应"的另一半原因。

★ 客户端对不在 itemData 里的 id 会打 error（RenderBag 时），所以只推能入包的 id；
  非道具奖励由 report 落盘，不进这条消息。
"""
from typing import Any

from shared.log_tee import get_logger

from ... import ms as msmod

_log = get_logger(__name__)

REWARD_SC = ("Avatar_Reward_SC", 882639301)
REWARD_SOURCE_SC = ("Avatar_Reward_Source_SC", 1757110621)


def _reward_map(granted: list[tuple[int, int, list[str]]]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for item_id, count, grids in granted:
        if count <= 0:
            continue
        info: dict[str, Any] = {"num": int(count)}
        gids = [str(g) for g in grids if g]
        if gids:
            info["gridids"] = gids
        out[int(item_id)] = info
    return out


def reward_frames(granted: list[tuple[int, int, list[str]]]) -> list[bytes]:
    """构造 Avatar_Reward_SC 推送帧（没有可展示内容时返回空列表）。"""
    m = _reward_map(granted)
    if not m:
        return []
    name, index = REWARD_SC
    pkt = msmod.build_entity_message(
        entity_id="", method_name=name, index=index,
        params=msmod.mp_arr([m]))   # mp_any 会递归编码 dict/list
    _log.info("[bag] 推送获得展示：%s 种道具", len(m))
    return [pkt]
