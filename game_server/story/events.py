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
        **检测入口**。业务只发事件，不直接改任务状态。

    注意用词：apply_event 只**检测**哪些任务可以完成，
    真正落状态的是 entity_rpc._finish_story_tasks -> story.finish_story_task。
    所以它是「检测入口」，不是「写入口」——
    2026-10-21 审计指出原文档写「唯一写入口」是夸大且自相矛盾。

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

    # 2) 事件型任务（TaskType 3 / 7）
    #    ★ 审计 P1-6：mark_seen 必须在求值**之后** ——
    #      之前是先标记再求值，求值抛异常的话事件就被永久吞掉了。
    try:
        hits, _ = _story.evaluate_event(st, event.type, event.params, now)
    except Exception as e:
        print("[story] 事件求值失败 %s: %s" % (event.type, e), flush=True)
        hits = []
    _mark_seen(st, event.event_id, now)

    # 3) 链式任务（TaskType 6）—— 任何事件之后都重新检查一次，
    #    因为上一步可能刚完成某个前置任务
    try:
        chain, _ = _story.check_chain_tasks(st, now)
    except Exception as e:
        print("[story] 链式检查失败: %s" % e, flush=True)
        chain = []

    return EventResult(hits=hits, chain_hits=chain, event=event)


def tick(st, now=None):
    """服务端主动推进入口。

    不依赖任何 RPC，只做「重新检查一遍链式任务」——
    用于登录、切场景、定时器等时机，把之前漏掉的推进补上。
    """
    now = int(now if now is not None else time.time())
    try:
        chain, _ = _story.check_chain_tasks(st, now)
    except Exception as e:
        print("[story] tick 链式检查失败: %s" % e, flush=True)
        chain = []
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
        # ★ 审计 P1-3：TaskEventData 里 StreetInteract 有 7/11 的 KeyList 是
        #   ["actorType","streetId"]，不传就永远命中不了。
        "actorType": int(actor_type) if actor_type is not None else 0,
    }
    if street_id is not None:
        params["streetId"] = int(street_id)
    if actor_type is not None:
        params["actorType"] = int(actor_type)
    return GameEvent(EV_STREET_INTERACT, params, source=source,
                     event_id="StreetInteract:%d:%d" % (int(actor_id),
                                                        int(interact_id)))


def ev_submit_item(item_id=0, count=1, submit_id=None, item_ids=None,
                   source="rpc:SubmitItem_CS"):
    """提交道具。

    ★ 客户端真实签名（avatarbagcomp.lua:930 / interactfunctionfactory.lua:810）：
        SubmitItem_CS(submit_id, itemDict)
      submit_id 是 **ItemSubmitData 的字符串键**（如 zhaixi_lv1Elevator），
      itemDict 是 {itemId: count}。
      之前把 vals[0] 当 itemId 用 int() 转，字符串会抛异常被吞掉，
      等于丢掉了 submit_id 这个关键信息。

    实测 TaskEventData 里 SubmitItem 的 EventParam 是**空的**（KeyList 为 nil），
    所以 event_matches 对任何 SubmitItem 事件都返回 True。
    也就是说：15 条 SubmitItem 剧情任务，理论上发一次就会全命中。
    实际上它们是同一剧情线的连续任务（PreTask 串起来），同一时刻只会有 1 条在进行中，
    所以危害有限；但这是**数据层面的限制**，不是我们能补的。

    注意：**不带 event_id** —— 同一个道具可以反复提交（每次都是真发生）。
    """
    p = {}
    if item_id:
        p["itemId"] = int(item_id)
    if count:
        p["count"] = int(count)
    if submit_id is not None:
        p["submit_id"] = submit_id
    if item_ids:
        p["itemIds"] = list(item_ids)
    return GameEvent(EV_SUBMIT_ITEM, p, source=source)


def ev_avatar_attr(attr, value, source="internal"):
    """角色属性变化。

    实测 TaskEventData 里 AvatarAttr 的 KeyList 是 ["level"]，
    所以等级变化要发 {"level": N}。

    ★ 2026-10-21 审计 P0-4：**不能按 level 去重**。
      7006 和 7007 两个剧情任务共用同一个 level=3 事件（TaskEventData[40103]），
      7006 完成后 7007 才激活，那时需要「再来一次 level=3 事件」——
      如果按 AvatarAttr:level:3 去重，7007 永远完不成。
      所以这里**不带 event_id**（每次真发生都算）。
    """
    p = {attr: value}
    if attr != "level":
        p["level"] = value
    return GameEvent(EV_AVATAR_ATTR, p, source=source)


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
