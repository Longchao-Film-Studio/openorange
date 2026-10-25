#!/usr/bin/env python3
"""副本上下文（P0-E2 第 3 步）。

进副本 / 切关卡时把「当前副本 / 关卡 / 区域」记到 _oo_ 私有字段，
结算时喂给剧情引擎。为什么需要：FinishDungeon 事件里
  - 57 个带 areaid（区域 id）
  - 4 个带 levelId（关卡 id）
而 story.event_matches 要求事件配置里的**每个**参数都出现在我们给的 params 里，
否则不匹配 —— 所以这些值必须提前记下来。
"""
from .. import gamedata
# SystemType.MainStory = 1004（序章就是这个）
_STORY_TYPE_MAINSTORY = 1004

def _remember_dungeon(st, copy_id, level_id=None):
    """记住「当前副本 / 关卡 / 区域」，结算时喂给剧情引擎。

    为什么需要：FinishDungeon 的事件里，
      - 57 个带 areaid（区域 id）
      - 4 个带 levelId（关卡 id）
    而 story.event_matches 要求事件配置里的**每个**参数都出现在我们给的
    params 里，否则不匹配。所以这些值必须提前记下来。

    ★ areaid 用 copyId 查 MainlineData（不是按关卡反查 —— 一个关卡可能被多个
      副本共用，按关卡反查会张冠李戴，详见 gamedata.area_of_copy 的注释）。
    """
    area = gamedata.area_of_copy(copy_id)
    if area:
        st.props["_oo_cur_area"] = int(area)
    else:
        st.props.pop("_oo_cur_area", None)
    try:
        lv = int(level_id) if level_id else 0
    except (TypeError, ValueError):
        lv = 0
    if lv:
        st.props["_oo_cur_level"] = lv
    else:
        st.props.pop("_oo_cur_level", None)
