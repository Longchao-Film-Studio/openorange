#!/usr/bin/env python3
"""剧情短信（NPC 手机对话）领域层 —— 状态写入口唯一（R8）。

背景与契约见 docs/方案-剧情短信.md / docs/计划-剧情短信RPC.md。

## 客户端链路（已配好，不需要我们写客户端代码）

  服务端写 npc_chat_prop.address_list[chatId][topicId]（新键 OP_ADD）
    -> 客户端 entryAdded 回调 on_NpcMessageTopic_Unlock_Change（config.lua:8588 附近）
    -> OpenNpcChatTopic -> 弹「收到短信」提示
  npc_chat_prop.resident_topic_ids（IntList）是未读/驻留队列，
    客户端 HasUnreadTopic() = #resident_topic_ids > 0（avatarnpcchatcomp.lua:55-63）

## Gate 0 结论（2026-10-25）

任务 -> 话题的映射**在数据与客户端代码里都不存在**：
  · NpcMessageData 字段全集 = chatName/chatType/dialogueId/id/importantLevel/pickUp/role
  · StoryTaskData[1044] 无话题字段
  · StorySpeechData/LanguageData/SysTextData 里 10490 的命中均为无关 id 子串（查实）
  · 客户端 mobilechat/storytask 组件没有「按任务查话题」的函数
=> 官方在服务端脚本里写死。我们自建 SMS_BY_TASK，并配两条防线：
  · 表外话题任务路径一律不解锁（INV-SMS-5）
  · 任务开始时若映射缺失，进未映射告警清单（P7，见 _unmapped_hit）

## 不变量（tests/test_npc_sms.py 逐条可红）

  INV-SMS-1  新增键必须 OP_ADD（Tx 自动判定，测试断言）
  INV-SMS-3  同一话题只解锁一次（幂等，重复调用返回空帧）
  INV-SMS-5  任务路径不得解锁映射表外话题（GM 路径显式 allow_unmapped）
  INV-SMS-6  未解锁话题收到进度上报 -> 忽略
  INV-SMS-7  is_over=1 后话题必须离开未读队列
  INV-SMS-8  建条目必须带非 0 update_time
  INV-SMS-9  新建条目必须自带 __id__（嵌套对象不能指望 Tx 递归补，
             否则客户端 CustomDict 断言失败 -> 整号登不上）
"""
import os
import time

from shared.log_tee import get_logger

from .. import gamedata
from ..property import ids as _ids
from ..property.tx import Tx

_log = get_logger(__name__)

# ---------------------------------------------------------------- 映射

# 任务 -> 话题。★ 只放人工核实过的条目（核对方法见 docs/方案-剧情短信.md §6）：
#   1044「接到杜老爷消息」旁白原文「你接到了杜老爷的短信」；
#   话题 1014 chatName="杜老爷短信"，首句「导演阁下，速回龙潮阁。」，
#   与任务同章节(main_01)同角色(杜老爷 100103)。
POST_SMS_BY_TASK = {
    1044: 1014,
}

# 支持的话题类型（1=私聊）。3=来电、群聊等结构已留（见 spec_data.json），
# 但解锁路径**显式拒绝**并告警 —— 宁可没短信，也不发错误的帧。
SMS_ONLY_CHAT_TYPES = (1,)

# 未映射告警去重（进程内；每次登录重启清零是可接受的，日志还有 tsv 全量）
_UNMAPPED_SEEN: set[int] = set()


def _unmapped_tsv_path():
    from .. import config
    return os.path.join(getattr(config, "LOG_DIR", "var/logs"),
                        "sms_unmapped.tsv")


def topics_for_task(task_id):
    """任务 -> 话题 id 列表。★ 映射唯一读取点（Gate 0 可替换实现）。"""
    tid = int(task_id)
    t = POST_SMS_BY_TASK.get(tid)
    return [t] if t else []


def _unmapped_hit(task_id):
    """任务有进行中但映射表没有 -> 告警 + 落盘（P7）。

    落盘：work/logs/sms_unmapped.tsv，格式 ts / task_id / scene / desc
    （与 unknown_requests.tsv 同风格，用来排"下一个该补哪条映射"）。
    进程内去重：同一任务一次进程只记一条。
    """
    if task_id in _UNMAPPED_SEEN:
        return
    _UNMAPPED_SEEN.add(task_id)
    _log.warning("[sms] 任务 %s 无短信映射（如官方此处有短信请补 POST_SMS_BY_TASK）", task_id)
    try:
        cfg = gamedata.story_task(task_id) or {}
        desc = "".join(str(x) for x in (cfg.get("Description") or []))
        line = '{}\t{}\t{}\t{}'.format(int(time.time()), task_id, cfg.get("GuideSceneId"), desc)
        path = _unmapped_tsv_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write("ts\ttask_id\tscene\tdesc\n")
            f.write(line + "\n")
    except Exception as e:
        _log.warning("[sms] 未映射清单落盘失败: %s", e)


# ---------------------------------------------------------------- 读取

def _topic_cfg(topic_id):
    cfg = gamedata.npc_message(topic_id)
    if not isinstance(cfg, dict):
        return None
    return cfg


def _chat_id_of(cfg):
    """私聊的 chatId = 话题 role（npcchatprop.lua:6 用 config.role 作键）。"""
    try:
        return int(cfg.get("role"))
    except Exception:
        return 0


def _entry(chat_type, npc_id, now, props):
    """一条 NpcChatInfo。形状 = config.lua:4925-4962 + npcchatprop.lua:5-14，
    并补上客户端模板漏写的 is_over/update_time（客户端会话列表按 update_time 排序）。

    ★ __id__ 必须在这里显式分配（2026-10-26 事故，123tst 整号登不上）：
      Tx._ensure_id 只给「直接传给 set() 的那个对象」补 id，**不递归子对象**。
      而"首次解锁"分支一次 set 塞了「会话记录 + 话题条目」两层 ——
      外层记录由 Tx 补了 id，内层 NpcChatInfo 没有。落盘后客户端每次登录
      重建 NpcChatProp，CustomDict 的 assert(id ~= nil) 失败 ->
      ClientAvatar:init 抛异常 -> 回登录界面（可复现的登录死循环）。
      嵌套新建的自定义对象（NpcChatRecord/NpcChatInfo 都继承 CustomDict）
      必须自己带 __id__，这是 Tx 的契约边界。
    """
    return {
        "__id__": _ids.alloc_id(props),
        "chat_type": int(chat_type),
        "npc_id": int(npc_id),
        # ★ group_npc_ids / topic_infos 声明是 IntDictKeyInt，客户端把它当 CustomDict
        #   （intdictkeyint.lua: class.Class("IntDictKeyInt", CustomDict)）—— 同样需要 __id__。
        #   2026-10-26 第二轮：只修了条目本身，客户端接着在 group_npc_ids 上断言失败。
        "group_npc_ids": {"__id__": _ids.alloc_id(props)},
        "is_over": 0,
        "update_time": int(now),
        "topic_infos": {"__id__": _ids.alloc_id(props)},
    }


def is_unlocked(st, chat_id, topic_id):
    al = ((st.props.get("npc_chat_prop") or {}).get("address_list") or {})
    conv = al.get(int(chat_id))
    return isinstance(conv, dict) and int(topic_id) in conv



# ---------------------------------------------------------------- 写入

# ---------------------------------------------------------------- 容器写入口

#: entryAdded 声明里「通配在末尾」的容器 —— 往这些容器的新增必须发父层 OP_ADD。
#: 依据 generated/config.lua 的 PropertyCallbacks.entryAdded：
#:   ["npc_chat_prop.address_list.*"] = on_NpcMessageTopic_Unlock_Change   (L8578)
#:   ["npc_chat_prop.call_records.*"] = on_NpcPhoneCallTopic_Unlock_Change (L8582)
#: 客户端 _getPropertyCallback 是按路径字符串精确查表的（星号只是
#: 「不定键容器」的后缀，不是通配符），所以层级差一段就永远不触发。
ENTRY_ADDED_CONTAINERS = {
    "address_list": "npc_chat_prop.address_list.*",
    "call_records": "npc_chat_prop.call_records.*",
}


def put_child_in_loaded_container(st, tx, container, chat_id, topic_id, entry):
    """往 npc_chat_prop[container][chat_id] 里放一条 topic，返回 tx。

    ★ 这是**唯一**允许往这两个容器写话题的地方。

    为什么必须「删键 + 整条重塞」而不是直接写 topic 那一层：
      entryAdded 声明的是 `npc_chat_prop.<container>.*`（通配在末尾），
      它只在 <chat_id> 这个键**新增**时触发一次。往已存在的键里塞 topic，
      客户端算出的回调路径会多一段 `.*`，精确查表失败 -> 回调不执行 ->
      玩家那边表现为「短信不弹」（2026-11-04 事故）。

    ★ 不能写成 `tx.delete(...)` + `tx.set(...)`：那会多发一条 OP_DEL，
      客户端会先短暂丢条目。这里直接在 props 上删旧键、让同一次 set 判成
      新增（tx.py set 里 `is_new = key not in parent`），全程只有一条 OP_ADD。

    ★ 值必须带上旧条目：客户端收到 OP_ADD 会用 createCustomObjRO 整体重建
      这个 NpcChatRecord，旧话题不在值里就丢了。
    """
    if container not in ENTRY_ADDED_CONTAINERS:
        raise ValueError(f"容器 {container!r} 不在 ENTRY_ADDED_CONTAINERS 白名单里")
    npc_prop = st.props.get("npc_chat_prop")
    box = npc_prop.get(container) if isinstance(npc_prop, dict) else None
    if not isinstance(box, dict):
        raise TypeError(f"npc_chat_prop.{container} 不存在或类型不对")
    chat_id = int(chat_id)
    if chat_id in box:
        existing = box[chat_id]
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged[int(topic_id)] = entry
        del box[chat_id]
        tx.set(("npc_chat_prop", container, chat_id), merged)
    else:
        tx.set(("npc_chat_prop", container, chat_id), {int(topic_id): entry})
    return tx


def unlock_topic(st, chat_id, topic_id, source="", allow_unmapped=False):
    """解锁一个短信话题（唯一写入口）。返回同步帧列表（幂等：已解锁返回 []）。

    allow_unmapped：GM/管理路径显式越权（跳过 INV-SMS-5 的映射检查并打日志）；
    任务路径**必须**保持默认 False。
    """
    topic_id = int(topic_id)
    chat_id = int(chat_id)
    cfg = _topic_cfg(topic_id)
    if cfg is None:
        _log.warning("[sms] 话题 %s 不在 NpcMessageData，拒绝解锁（%s）", topic_id, source)
        return []
    chat_type = int(cfg.get("chatType") or 0)
    if chat_type not in SMS_ONLY_CHAT_TYPES:
        _log.warning("[sms] 话题 %s chatType=%s 不支持，拒绝解锁（%s）",
                     topic_id, chat_type, source)
        return []
    if not gamedata.npc_chat_dialog(topic_id):
        _log.warning("[sms] 话题 %s 没有对话行，拒绝解锁（%s）", topic_id, source)
        return []
    if not allow_unmapped and topic_id not in POST_SMS_BY_TASK.values():
        _log.warning("[sms] 话题 %s 不在映射表，任务路径拒绝解锁（%s）", topic_id, source)
        return []
    if is_unlocked(st, chat_id, topic_id):
        return []          # INV-SMS-3：幂等

    real_chat = _chat_id_of(cfg) or chat_id
    now = int(time.time())
    tx = Tx(st.props)
    put_child_in_loaded_container(
        st, tx, "address_list", real_chat, topic_id,
        _entry(chat_type, real_chat, now, st.props),
    )
    # 未读队列（IntList；Tx.append 发 OP_INSERT）
    try:
        tx.append(("npc_chat_prop", "resident_topic_ids"), topic_id)
    except Exception as e:
        _log.warning("[sms] resident_topic_ids 追加失败（%s）: %s", topic_id, e)
    st.save()
    _log.info("[sms] 解锁话题 %s chat=%s（%s）", topic_id, real_chat, source)
    return tx.frames()


def unlock_for_task(st, task_id, source="task"):
    """任务开始时的钩子入口：查映射并解锁。返回帧列表。

    ★ 任何失败都不允许影响任务推进 —— 调用方拿到的是帧，异常在这里吞掉但必须告警。
    """
    try:
        tids = topics_for_task(task_id)
        if not tids:
            _unmapped_hit(task_id)
            return []
        frames = []
        for tid in tids:
            cfg = _topic_cfg(tid) or {}
            chat_id = _chat_id_of(cfg)
            frames.extend(unlock_topic(st, chat_id, tid,
                                       source=f"{source}:task{task_id}"))
        return frames
    except Exception as e:
        _log.warning("[sms] 任务 %s 短信解锁失败: %s", task_id, e)
        return []


def mark_topic_progress(st, chat_id, topic_id, info_id, info_val, is_over):
    """记录玩家选择 / 完成状态（Update_NpcChat_Info_CS 的落库）。

    未解锁的话题一律忽略（INV-SMS-6）。is_over=1 时话题出未读队列（INV-SMS-7）。
    """
    topic_id = int(topic_id)
    chat_id = int(chat_id)
    if not is_unlocked(st, chat_id, topic_id):
        _log.warning("[sms] 进度上报：话题 %s 未解锁，忽略（chat=%s）", topic_id, chat_id)
        return []
    now = int(time.time())
    tx = Tx(st.props)
    conv = st.props["npc_chat_prop"]["address_list"][chat_id]
    entry = conv[topic_id]
    infos = dict(entry.get("topic_infos") or {})
    infos[int(info_id)] = int(info_val)
    # ★ 必须写到 [chat_id][topic_id] 这一层（NpcChatInfo 的字段）。
    #   2026-10-26：以前少了 topic_id 一层，客户端把 is_over / topic_infos 当成
    #   "新的话题条目"去构造 NpcChatInfo（值是 int 或重复的 __id__），
    #   打 "NpcChatInfo attach to cache, id repeated!"，并把脏键写进存档 ——
    #   下次登录时客户端在 ClientAvatar:init 崩（整号登不上）。
    tx.set(("npc_chat_prop", "address_list", chat_id, topic_id, "topic_infos"), infos)
    tx.set(("npc_chat_prop", "address_list", chat_id, topic_id, "is_over"), int(is_over))
    tx.set(("npc_chat_prop", "address_list", chat_id, topic_id, "update_time"), now)
    if int(is_over) == 1:
        _remove_unread(tx, st, topic_id)
    st.save()
    _log.info("[sms] 进度 chat=%s topic=%s info=%s=%s over=%s",
              chat_id, topic_id, info_id, info_val, is_over)
    return tx.frames()


def _remove_unread(tx, st, topic_id):
    """从未读队列移除一个话题（按值找位置，OP_REMOVE）。"""
    lst = st.props.get("npc_chat_prop", {}).get("resident_topic_ids")
    if not isinstance(lst, dict):
        return
    for pos, v in list(lst.items()):
        try:
            if int(v) == int(topic_id):
                tx.remove_at(("npc_chat_prop", "resident_topic_ids"), int(pos))
                return
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
