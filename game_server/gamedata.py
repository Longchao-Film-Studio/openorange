#!/usr/bin/env python3
"""game_server.gamedata —— 只读访问客户端的 BDD 数据表。

为什么需要它
------------
服务端要判断「某个道具用了会怎样」「体力上限是多少」，
这些规则全在客户端的 BDD 表里（ItemData / CommonConstData / LevelExpData …）。

路径
----
BDD 表不在仓库里（65 MB，来自游戏安装目录）。默认按
<repo>/../archive/game_data/BDD 找，可用环境变量 OO_BDD_DIR 或 config.BDD_DIR 覆盖。

**找不到时不会崩**：所有查询返回 None / 默认值，并只警告一次。
这样服务端在没装数据的环境里也能跑（只是没有道具效果）。

用法
----
    from . import gamedata
    gamedata.item(110)            # -> {'useType': 'avatarAttr', 'useParam': {...}, ...}
    gamedata.const('KeyNumMax')   # -> 180
    gamedata.level_exp(10)        # -> {'maxStaminaAdd': ..., ...}
"""
import json
import os
import threading

from shared.log_tee import get_logger

from . import config

_log = get_logger(__name__)

_LOCK = threading.RLock()
_CACHE = {}
_WARNED = set()


def _path(name):
    return os.path.join(config.BDD_DIR, "TableData.CommonData.{}.json".format(name))


def table(name):
    """按表名取整张表（dict 或 list）。读不到返回 None。"""
    with _LOCK:
        if name in _CACHE:
            return _CACHE[name]
        p = _path(name)
        data = None
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                if name not in _WARNED:
                    _WARNED.add(name)
                    _log.warning("[data] {} 读取失败: {}".format(name, e))
        else:
            if "dir" not in _WARNED:
                _WARNED.add("dir")
                print("[data] 警告：找不到 BDD 目录 {} —— 道具效果/体力恢复等规则不可用。".format(config.BDD_DIR), flush=True)
        _CACHE[name] = data
        return data


def _plain(entry, key, default=None):
    """BDD 的 OrderedDict 里混着 __bdd_metatype__ / KeyList，取纯值时过滤掉。"""
    if not isinstance(entry, dict):
        return default
    v = entry.get(key)
    if isinstance(v, dict):
        return v
    return default if v is None else v


def item(item_id):
    """ItemData[item_id]。"""
    d = table("ItemData")
    if not d:
        return None
    v = d.get(str(item_id)) or d.get(item_id)
    return v if isinstance(v, dict) else None


def item_use_param(item_id):
    """道具的使用参数：{TowerRewardKeys: 60, ...}（去掉 BDD 元数据键）。"""
    it = item(item_id)
    if not it:
        return {}
    up = it.get("useParam") or {}
    if not isinstance(up, dict):
        return {}
    return {k: v for k, v in up.items()
            if k not in ("KeyList", "__bdd_metatype__")}


def item_use_type(item_id):
    it = item(item_id)
    return (it or {}).get("useType")


def const(key, default=None):
    """CommonConstData[key]。"""
    d = table("CommonConstData")
    if not d:
        return default
    v = d.get(key)
    return default if v is None else v


def level_exp(level):
    """LevelExpData[level]（玩家等级配置，含 maxStaminaAdd）。

    注意：这张表在 BDD 里是 **list**（不是 dict），每条带 id 字段。
    """
    d = table("LevelExpData")
    if not d:
        return None
    if isinstance(d, dict):
        v = d.get(str(level)) or d.get(level)
        return v if isinstance(v, dict) else None
    for v in d:
        if isinstance(v, dict) and v.get("id") == level:
            return v
    return None


def char(char_id):
    """CharData[char_id]（角色配置：DefaultSkin / charAppearance / charName…）。"""
    return row("CharData", char_id)


def char_attr(char_id):
    """CharAttrData[char_id]（46 字段战斗基础属性）。"""
    return row("CharAttrData", char_id)


def char_upgrade(char_id):
    """CharUpgradeData[char_id] -> 等级列表（99 级，每级含 costExp/成长）。

    ★ 嵌套结构：{charId: [等级列表]}，下标 0 = 1 级。
    """
    d = table("CharUpgradeData")
    if not isinstance(d, dict):
        return None
    v = d.get(str(char_id)) or d.get(char_id)
    return v if isinstance(v, list) else None


def char_break(char_id):
    """CharBreakData[char_id] -> 突破阶列表（10 阶）。"""
    d = table("CharBreakData")
    if not isinstance(d, dict):
        return None
    v = d.get(str(char_id)) or d.get(char_id)
    return v if isinstance(v, list) else None


def char_rising_star(char_id):
    """CharRisingStarData[char_id] -> 升星列表（6 星）。"""
    d = table("CharRisingStarData")
    if not isinstance(d, dict):
        return None
    v = d.get(str(char_id)) or d.get(char_id)
    return v if isinstance(v, list) else None


def char_max_level(char_id):
    """角色等级上限 = 等级表长度。查不到返回 1。"""
    ups = char_upgrade(char_id)
    return len(ups) if isinstance(ups, list) and ups else 1


def char_appearance(skin_id):
    """CharAppearanceData[skin_id]（皮肤配置，含 charId 反查）。"""
    return row("CharAppearanceData", skin_id)


def story_task(task_id):
    d = table("StoryTaskData")
    if not d:
        return None
    v = d.get(str(task_id)) or d.get(task_id)
    return v if isinstance(v, dict) else None


def task(task_id):
    d = table("TaskData")
    if not d:
        return None
    v = d.get(str(task_id)) or d.get(task_id)
    return v if isinstance(v, dict) else None


def row(table_name, row_id):
    """按表名 + 行 id 取一行（字符串/整数键都试）。读不到返回 None。"""
    d = table(table_name)
    if not isinstance(d, dict):
        return None
    v = d.get(str(row_id))
    if v is None:
        v = d.get(row_id)
    return v



def npc_message(topic_id):
    """NpcMessageData[topic_id] —— 手机短信话题（chatName/role/chatType/dialogueId…）。"""
    return row("NpcMessageData", topic_id)


def npc_chat_dialog(topic_id):
    """NpcChatDialogData[topic_id] —— 话题的全部对话行（dict: dialogueId -> row）。

    行字段：order / chattext / chatrole / voiceStart…；BDD 元数据键过滤交给调用方。
    """
    d = table("NpcChatDialogData")
    if not isinstance(d, dict):
        return None
    v = d.get(str(topic_id)) if str(topic_id) in d else d.get(topic_id)
    return v if isinstance(v, dict) else None


def npc_actor(role_id):
    """NpcActorChatData[role_id] —— 聊天角色（actorName/actorIcon/signature）。"""
    return row("NpcActorChatData", role_id)


def story_line(story_id):
    """StoryLineData[story_id] —— 剧情段落，含 dungeonId（对应 Level_CopyData 的 copyId）。"""
    return row("StoryLineData", story_id)


def level(level_id):
    """LevelData[level_id] —— 关卡配置（levelName / scene / nextLevel）。"""
    return row("LevelData", level_id)


_AREA_BY_COPY = None


def area_of_copy(copy_id):
    """copyId -> 区域 id（FinishDungeon 事件里的 areaid）。

    ★ 必须用 MainlineData，不能用 EnterDungeonData 按关卡反查。

    依据（2026-10-21 审计修正）：
      MainlineData 每行 = {ID: 区域id, dungeonid: copyId, sectionname, ...}
      125 行 / 125 个不同 copyId，是 copyId -> areaid 的**直接**映射。

    为什么不用 EnterDungeonData（{DungeonId, StageId}）：
      StageId 确实等于 LevelData 的 id（108/109 对得上），但一个关卡可能被
      多个副本共用，按关卡反查会张冠李戴。实测 3 个副本会给出错误 areaid：
        dgarea_arcade_11  关卡 52108 -> 10304（错，应为 10311）
        dgarea_arcade_12  关卡 52109 -> 10309（错，应为 10312）
        dgarea_arcade_13  关卡 52123 -> 10310（错，应为 10313）
      错 areaid 会导致**误完成别人的剧情任务**（10304/10309/10310 各挂着
      事件 5009/5014/5015），比拿不到更糟。所以宁可不给，也不给错的。

    覆盖：125/322 个副本。其余 197 个在 MainlineData 里没有记录，返回 None，
    对应事件就不匹配（这是正确行为，等 P1 副本入口映射一起解决）。
    """
    global _AREA_BY_COPY
    if _AREA_BY_COPY is None:
        _AREA_BY_COPY = {}
        for row in (table("MainlineData") or {}).values():
            if not isinstance(row, dict):
                continue
            cid = row.get("dungeonid")
            aid = row.get("ID")
            if isinstance(cid, str) and isinstance(aid, int):
                _AREA_BY_COPY.setdefault(cid, aid)
    if not isinstance(copy_id, str):
        return None
    return _AREA_BY_COPY.get(copy_id)


def mainline(dungeon_id):
    """MainlineData[dungeon_id] —— 主线副本配置。

    字段：{ID: 区域id(=FinishDungeon 的 areaid), dungeonid: copyId,
           sectionname, dungeonType, awardItemList, ...}

    客户端 AvatarMatch:GetSpaceLevel（avatarmatch.lua:29-31）：
      system_type == Mainline 时，match_info.dungeonId 就是这张表的 ID，
      copyId = MainlineData[dungeonId].dungeonid。
    """
    return row("MainlineData", dungeon_id)


def level_copy(copy_id):
    """Level_CopyData[copy_id] —— 副本配置，含 rootLevel（关卡 id 列表）。"""
    return row("Level_CopyData", copy_id)


def gameplay(type_id, mode_id=None):
    """GameplayData 里按 typeId(+modeId) 找一条。序章是 typeId=1004, modeId=1。"""
    d = table("GameplayData")
    if not d:
        return None
    rows = d.values() if isinstance(d, dict) else d
    for v in rows:
        if not isinstance(v, dict):
            continue
        if v.get("typeId") != type_id:
            continue
        if mode_id is not None and v.get("modeId") != mode_id:
            continue
        return v
    return None


def space_task(task_id):
    """SpaceTaskData[taskId] —— 战斗内空间任务配置（按 taskId 全局唯一查）。

    ★ 为什么这个函数很关键（P1 阶段 J 取证结论）

      `SpaceTaskData` 是「副本内任务」表，嵌套结构是 {dungeonId: {taskId: row}}。
      实测 909 条 taskId **跨 58 个副本全局唯一**（0 重复），所以可以只按
      taskId 查，不需要先知道当前副本。

      字段：{id, dungeonId, levelId, finishType, avatarTask, avatarTaskType,
             actorList, stateList, PreTaskList, Tag, TaskName, ...}

    ★ `avatarTask` 是**剧情任务 id**：270 条 TaskType=0 的剧情任务
      **全部**被某个 SpaceTask.avatarTask 指向（100% 覆盖）。
      这就是 type0 的完成路径 —— 见 `story.space_task_avatar_task()`。
    """
    raw = table("SpaceTaskData") or {}
    try:
        tid = int(task_id)
    except Exception:
        return None
    for group in raw.values():
        if not isinstance(group, dict):
            continue
        row = group.get(str(tid))
        if isinstance(row, dict):
            return row
    return None


def space_task_of_avatar(story_task_id):
    """反查：哪个 SpaceTask 的 avatarTask 指向这个剧情任务。

    返回 SpaceTask 行（dict）或 None。同一剧情任务可能被多个 SpaceTask 指向
    （实测有重复），这里返回第一条（按 taskId 升序，保证可复现）。
    """
    raw = table("SpaceTaskData") or {}
    try:
        want = int(story_task_id)
    except Exception:
        return None
    hits = []
    for group in raw.values():
        if not isinstance(group, dict):
            continue
        for tid, row in group.items():
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get("avatarTask")) == want:
                    hits.append((int(tid), row))
            except Exception:
                continue
    if not hits:
        return None
    hits.sort(key=lambda x: x[0])
    return hits[0][1]


def task_data(task_id):
    """TaskData[task_id] —— 通用任务表，带 TriggerEventList（完成条件）。"""
    return row("TaskData", task_id)


def task_event(event_id):
    """TaskEventData[event_id] —— 触发条件，形如 {EventType:"FinishDungeon", EventParam:{...}}。"""
    return row("TaskEventData", event_id)


def street_treasure(treasure_id):
    """StreetTreasureTaskData[treasure_id] —— 街景宝箱配置。

    字段（实测）：
      AwardItemList = {道具id: 数量}
      AwardAttr     = {street_id, prestige_exp}
      OwnStreetID / StoryID / preTaskStateMap / treasureTaskType
    """
    return row("StreetTreasureTaskData", treasure_id)


def street_prestige(street_id):
    """StreetPrestigeData[street_id] —— 该街区的声望等级表（list，含 needExpNum/level/levelAward）。"""
    d = table("StreetPrestigeData")
    if not d:
        return []
    v = d.get(str(street_id)) or d.get(street_id)
    return v if isinstance(v, list) else []


def all_system_catalogue():
    d = table("AllSystemCatalogueData")
    if not d:
        return []
    return list(d.values()) if isinstance(d, dict) else list(d)


def clear_cache():
    with _LOCK:
        _CACHE.clear()