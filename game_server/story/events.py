# -*- coding: utf-8 -*-
"""game_server.story.events —— 事件总线（P0-D）。

## 要解决的问题

服务端只响应 RPC，没有主动推进。

story.evaluate_event 全仓库**只有 1 个调用点**（副本结算里写死 "FinishDungeon"）。
也就是说：**剧情事件只在「又打一次副本」时才会被求值。**
结果就是：交道具、升等级、拿道具、NPC 交互这些明明做了，剧情任务却纹丝不动。

## 设计

    GameEvent(type, params, source, event_id)
        type      事件类型（对应 TaskEventData.EventType）
        params    事件参数 dict
        source    事件来源，如 "rpc:Cmd_Avatar_FinishGame_CS" / "tick"
        event_id  去重键；None 表示不去重（每次发生都算）

    apply_event(st, event) -> EventResult
        **唯一写入口**。业务只发事件，不直接改任务状态。

## 为什么 apply_event 不直接落状态

踩过的坑（2026-10-20 阶段 2）：如果这里直接 finish_story_task，
外层 _finish_story_tasks 会因为任务已经是 Finished 而跳过，
导致**不发奖、不推同步帧** —— 客户端看不到变化。

所以 apply_event 只**检测**，落状态 + 发奖 + 推同步统一走
entity_rpc._finish_story_tasks。

## 去重

存在 props["_oo_event_seen"]：{event_id: 时间戳}。
只保留最近 _SEEN_MAX 条，避免存档无限增长。
"""
import time

from .. import gamedata
# ★ 用模块引用而不是 from . import xxx 绑定函数：
#   测试会 monkeypatch story.evaluate_event 来抓事件参数，
#   绑死了函数名就抓不到（test_dungeon_events.py 有 5 项因此失败）。
from .. import story as _story

# 去重记录存这个私有属性（_oo_ 前缀，登录时会被剥离，不会发给客户端）
_SEEN_KEY = "_oo_event_seen"
_SEEN_MAX = 512

# 事件类型常量（对应 TaskEventData.EventType 的字符串值）
EV_FINISH_DUNGEON = "FinishDungeon"
EV_FINISH_TASK = "FinishTask"
EV_STREET_INTERACT = "StreetInteract"
EV_SUBMIT_ITEM = "SubmitItem"
EV_AVATAR_ATTR = "AvatarAttr"
EV_GET_ITEM = "GetItem"
EV_CHAR_DATA = "CharData"

# P0-D 明确要覆盖的 4 种（合计覆盖 26 条剧情任务）
CORE_TYPES = (EV_SUBMIT_ITEM, EV_AVATAR_ATTR, EV_GET_ITEM, EV_CHAR_DATA)


class GameEvent(object):
    """一条游戏事件。"""

    __slots__ = ("type", "params", "source", "event_id")

    def __init__(self, type, params=None, source="", event_id=None):
        self.type = type
        self.params = params or {}
        self.source = source
        self.event_id = event_id

    def __repr__(self):
        return "GameEvent(%s, %r, src=%s, id=%s)" % (
            self.type, self.params, self.source, self.event_id)

    def to_dict(self):
        return {"type": self.type, "params": self.params,
                "source": self.source, "event_id": self.event_id}


class EventResult(object):
    """apply_event 的结果。

    hits        检测到可以完成的剧情任务 id 列表（TaskType 3/7）
    chain_hits  检测到可以完成的链式任务 id 列表（TaskType 6）
    deduped     是不是因为 event_id 重复被跳过
    """

    __slots__ = ("hits", "chain_hits", "deduped", "event")

    def __init__(self, hits=None, chain_hits=None, deduped=False, event=None):
        self.hits = hits or []
        self.chain_hits = chain_hits or []
        self.deduped = deduped
        self.event = event

    @property
    def all_hits(self):
        return list(self.hits) + list(self.chain_hits)

    def __repr__(self):
        return "EventResult(hits=%r, chain=%r, deduped=%r)" % (
            self.hits, self.chain_hits, self.deduped)


# ---------------------------------------------------------------- 去重
def _seen(st):
    m = st.props.get(_SEEN_KEY)
    if not isinstance(m, dict):
        m = {}
        st.props[_SEEN_KEY] = m
    return m


def already_seen(st, event_id):
    if not event_id:
        return False
    return event_id in _seen(st)


def _mark_seen(st, event_id, now):
    if not event_id:
        return
    m = _seen(st)
    m[event_id] = int(now)
    if len(m) > _SEEN_MAX:
        keep = sorted(m.items(), key=lambda kv: kv[1])[-_SEEN_MAX:]
        st.props[_SEEN_KEY] = dict(keep)


def clear_seen(st):
    """测试用：清空去重记录。"""
    st.props.pop(_SEEN_KEY, None)


# ---------------------------------------------------------------- 核心
def apply_event(st, event, now=None):
    """把一条事件喂进剧情系统，返回 EventResult。

    ★ 这是剧情推进的**唯一写入口**：业务代码只负责发事件，
      不要自己判断「该完成哪个任务」。
    """
    now = int(now if now is not None else time.time())
    if event is None:
        return EventResult(event=None)

    # 1) 去重
    if event.event_id and already_seen(st, event.event_id):
        return EventResult(deduped=True, event=event)
    _mark_seen(st, event.event_id, now)

    # 2) 事件型任务（TaskType 3 / 7）
    hits, _ = _story.evaluate_event(st, event.type, event.params, now)

    # 3) 链式任务（TaskType 6）—— 任何事件之后都重新检查一次，
    #    因为上一步可能刚完成某个前置任务
    chain, _ = _story.check_chain_tasks(st, now)

    return EventResult(hits=hits, chain_hits=chain, event=event)


def tick(st, now=None):
    """服务端主动推进入口。

    不依赖任何 RPC，只做「重新检查一遍链式任务」——
    用于登录、切场景、定时器等时机，把之前漏掉的推进补上。
    """
    now = int(now if now is not None else time.time())
    chain, _ = _story.check_chain_tasks(st, now)
    return EventResult(chain_hits=chain)


def pending_tasks(st):
    """列出当前「进行中但还没完成」的剧情任务，用于诊断。"""
    out = []
    for task_id, item in _story._active_story_tasks(st):
        cfg = gamedata.story_task(task_id) or {}
        out.append({
            "taskId": task_id,
            "taskType": cfg.get("TaskType"),
            "state": item.get("state"),
        })
    return out


# ---------------------------------------------------------------- 便捷发射器
def ev_dungeon_finish(type_id, mode_id=1, level=1, level_id=0, is_win=True,
                      first_pass=False, source="rpc:Cmd_Avatar_FinishGame_CS"):
    """副本结算。"""
    return GameEvent(EV_FINISH_DUNGEON, {
        "typeId": int(type_id or 0),
        "modeId": int(mode_id or 0),
        "level": int(level or 0),
        "levelId": int(level_id or 0),
        "isWin": 1 if is_win else 0,
        "first_pass": 1 if first_pass else 0,
    }, source=source)


def ev_finish_task(task_id, source="internal"):
    """某个任务完成（通用任务 / 剧情任务都算）。"""
    return GameEvent(EV_FINISH_TASK, {"taskId": int(task_id)},
                     source=source,
                     event_id="FinishTask:%d" % int(task_id))


def ev_street_interact(actor_id, interact_id, street_id=None, actor_type=None,
                       source="rpc:Street_Actor_InteractID_Update_CS"):
    """街区 NPC 交互。

    ★ 参数键必须和 TaskEventData 里的一致：**InteractId（大写 I）**。
      实测 TaskEventData 里 StreetInteract 的 KeyList 是 ["InteractId"] 或
      ["actorType", "streetId"]，写成 interactId 永远匹配不上。

    ★ event_id 用 actor+interact 组合：同一 NPC 的同一个交互只算一次，
      解决「重复交互会重复走一遍」的问题。
    """
    params = {
        "InteractId": int(interact_id),
        "interactId": int(interact_id),      # 兼容另一种写法
        "actorId": int(actor_id),
    }
    if street_id is not None:
        params["streetId"] = int(street_id)
    if actor_type is not None:
        params["actorType"] = int(actor_type)
    return GameEvent(EV_STREET_INTERACT, params, source=source,
                     event_id="StreetInteract:%d:%d" % (int(actor_id),
                                                        int(interact_id)))


def ev_submit_item(item_id=0, count=1, source="rpc:SubmitItem_CS"):
    """提交道具。

    实测 TaskEventData 里 SubmitItem 的 EventParam 是**空的**（KeyList 为 nil），
    所以 event_matches 对任何 SubmitItem 事件都返回 True ——
    任务完成靠的是「客户端只在真的提交时才会发这个 RPC」。

    注意：**不带 event_id** —— 同一个道具可以反复提交（每次都是真发生）。
    """
    return GameEvent(EV_SUBMIT_ITEM,
                     {"itemId": int(item_id), "count": int(count)},
                     source=source)


def ev_avatar_attr(attr, value, source="internal"):
    """角色属性变化。

    实测 TaskEventData 里 AvatarAttr 的 KeyList 是 ["level"]，
    所以等级变化要发 {"level": N}。
    """
    p = {attr: value}
    if attr != "level":
        p["level"] = value
    return GameEvent(EV_AVATAR_ATTR, p, source=source,
                     event_id="AvatarAttr:%s:%s" % (attr, value))


def ev_get_item(item_id=0, count=1, item_type=None, level=None, grade=None,
                source="rpc:RecordItemObtain_CS"):
    """获得道具。

    实测 TaskEventData 里 GetItem 有两种 KeyList：
      ["itemId"]                        -> 只要 itemId
      ["itemType", "level", "grade"]    -> 道具类型/等级/品质
    两种参数都带上，让 event_matches 按任务自己的需求取。
    """
    p = {"itemId": int(item_id), "count": int(count)}
    if item_type is not None:
        p["itemType"] = int(item_type)
    if level is not None:
        p["level"] = int(level)
    if grade is not None:
        p["grade"] = int(grade)
    return GameEvent(EV_GET_ITEM, p, source=source)


def ev_char_data(level=None, ascend=None, char_id=None, source="internal"):
    """角色数据（等级 / 突破）。

    实测 TaskEventData 里 CharData 的 KeyList 是 ["level"] 或 ["ascend"]。
    """
    p = {}
    if level is not None:
        p["level"] = int(level)
    if ascend is not None:
        p["ascend"] = int(ascend)
    if char_id is not None:
        p["charId"] = int(char_id)
    return GameEvent(EV_CHAR_DATA, p, source=source,
                     event_id="CharData:%s" % ("/".join(
                         "%s=%s" % (k, p[k]) for k in sorted(p))))
