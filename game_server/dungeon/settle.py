#!/usr/bin/env python3
"""副本结算（P0-E2 第 3 步）。

完整五步链路（见 ROADMAP 2-4 节）：
  ① 客户端上报 finishInfo（含 toStreetId / toStreetPosId / 每人 endResult）
       Cmd_Avatar_FinishGame_CS   —— 离线/Avatar 路径（dispatchhubonline.lua:136-151）
       Cmd_OfflineSpace_Finish_CS —— Soul 路径
  ② 服务端算奖励
  ③ 服务端下发 AvatarSpace_SpaceFinish_SC（index 1634212414）  ← 关键缺口
  ④ 客户端按 match_info.system_type 分派：
       1004(StoryLine) -> storyLineSettle()，只调 directLeaveBattle()，**不弹面板**
       1000(Dungeon)   -> dungeonChallengeSettle() 才弹面板
       序章是 1004，所以打完是「直接回街区」而不是弹面板 —— 这是正常的。
  ⑤ 玩家退出 -> GoBackToWhere -> 用 toStreetId/toStreetPosId 回街区

之前只回了个 ACK，客户端 onFinishGameSuccess(true) 什么都不做
（offlinebattlespace.lua:217-221），于是卡在战斗空间、输入被锁。
"""
from shared.log_tee import get_logger

from .. import ms as msmod
from .. import story as story_mod
from ..rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame
from ..story import events as _events
from ..story.rpc import _finish_story_tasks
from ..story.tasks import drain_event_task_sync
from .context import _STORY_TYPE_MAINSTORY
from .reward import _grant_battle_items

_log = get_logger(__name__)
RPC_AVATAR_SPACE_FINISH_SC = 1634212414   # AvatarSpace_SpaceFinish_SC

def _finish_info_of(info, uid):
    """从 finishInfo 里取出「我」那一份 endResult。"""
    if not isinstance(info, dict):
        return {}
    me = info.get(uid)
    if isinstance(me, dict):
        return me
    for v in info.values():
        if isinstance(v, dict) and ("battle_items" in v or "win" in v):
            return v
    return {}

def _settlement_table(st, uid, info, reward_list):
    """构造下发客户端的结算表（AvatarSpace_SpaceFinish_SC 的唯一参数）。

    客户端读的地方：
      avatarsettlementcomp.lua:54-76  remakeResultData
        -> resultData[uid].reward_item_list / useTime / collectCount / win
        -> resultData.userList（遍历找自己）
      avatarsettlementcomp.lua:154-178 GoBackToWhere
        -> data.toStreetId / data.toStreetPosId
      modules/battlesettle/settlefactory.lua:327-335 createSettle
        -> resultData.match_info.system_type
           1004(StoryLine) -> storyLineSettle（不弹面板，直接回街区）
           1000(Dungeon)  -> dungeonChallengeSettle（弹面板）
    """
    me = _finish_info_of(info, uid)
    to_street = info.get("toStreetId") if isinstance(info, dict) else None
    to_pos = info.get("toStreetPosId") if isinstance(info, dict) else None
    try:
        to_street = int(to_street) if to_street is not None else None
    except Exception:
        to_street = None
    if not to_street:
        to_street = int(st.props.get("street_id") or 0)
    try:
        to_pos = int(to_pos) if to_pos is not None else 0
    except Exception:
        to_pos = 0
    try:
        finish_reason = int(info.get("finishReason") or 0)
    except Exception:
        finish_reason = 0
    mine = {
        "win": bool(me.get("win", True)),
        "reward_item_list": reward_list,
        "useTime": int(me.get("useTime") or 0),
        "collectCount": int(me.get("collectCount") or 0),
        "rank": int(me.get("rank") or 1),
        "damage": int(me.get("damage") or 0),
    }
    match_info = st.props.get("_oo_cur_match")
    if not isinstance(match_info, dict):
        match_info = {"system_type": 1004, "modeId": 1, "match_type": 3}
    settle = {
        "userList": {uid: 1},
        uid: mine,
        "toStreetId": to_street,
        "toStreetPosId": to_pos,
        "finishReason": finish_reason,
        "match_info": match_info,
    }
    return settle

def _advance_story_on_finish(st, info=None, uid=None):
    """副本打完后推进剧情。

    优先用 finishInfo 自带的 typeId/modeId/level（兼容手工构造的调用）；
    没有就用 StartGame 时存下的 _oo_cur_match（match_info）反查：
    typeId = system_type，modeId = modeId。
    序章就是 FinishDungeon{typeId:1004, modeId:1, level:1}。
    """
    type_id = 0
    mode_id = 0
    level = 1
    # ★ P1 阶段 N（审计 P0-4 修复）：只信服务端 StartGame 记的上下文，
    #   不再用客户端 finishInfo 里的 typeId/modeId/level 构造事件。
    #
    #   漏洞：以前 finishInfo 优先，攻击者发一个
    #     Cmd_OfflineSpace_Finish_CS {typeId:1004, modeId:1, level:1}
    #   就能在没进任何副本时伪造 FinishDungeon{typeId:1004}，
    #   直接完成 type3 的序章任务 1000（审计实测可行）。
    #
    #   语义：一次结算必然对应一次 StartGame。没有 _oo_cur_match 就说明
    #   「这个结算不属于任何已开始的副本」，不应该推进剧情。
    #   客户端 finishInfo 只用于战斗内统计（道具/胜负/用时），不参与事件构造。
    # ★ typeId 严格只信服务端（它决定「哪个副本的事件能命中」，是安全关键）；
    #   modeId / level 缺失时才回退到客户端 finishInfo（它们只是战斗模式/关卡序号，
    #   在 typeId 已固定的前提下无法伪造成别的副本）。
    m = st.props.get("_oo_cur_match")
    if isinstance(m, dict):
        try:
            type_id = int(m.get("system_type") or 0)
        except Exception:
            type_id = 0
        try:
            mode_id = int(m.get("modeId") or 0)
        except Exception:
            mode_id = 0
        try:
            level = int(m.get("level") or 0)
        except Exception:
            level = 0
    if not type_id:
        print("[dungeon] 结算没有服务端副本上下文（未 StartGame），不推进剧情",
              flush=True)
        return []
    # 回退：match_info 没给的字段用 finishInfo 补（不覆盖服务端已有的值）
    if isinstance(info, dict):
        if not mode_id:
            try:
                mode_id = int(info.get("modeId") or 0)
            except Exception:
                mode_id = 0
        if not level:
            try:
                level = int(info.get("level") or 0)
            except Exception:
                level = 0
    if not level:
        level = 1
    # ★ 把能拿到的参数都给上。event_matches 要求事件配置里的每个键都在
    #   params 里，所以参数越全，能匹配上的事件越多：
    #     areaid        57 个事件需要（区域 id，用 copyId 查 MainlineData）
    #     levelId        4 个事件需要（具体关卡 id）
    #     instructionid  6 个事件需要（战斗教学指令 id）
    #     first_pass     6 个事件需要（首通标记）
    #     isWin          1 个事件需要
    ev = {"typeId": type_id, "modeId": mode_id, "level": level}
    area = st.props.get("_oo_cur_area")
    if area:
        ev["areaid"] = int(area)
    lv = st.props.get("_oo_cur_level")
    if lv:
        ev["levelId"] = int(lv)
    teach = st.props.get("_oo_cur_teach")
    if teach:
        ev["instructionid"] = int(teach)
    me = _finish_info_of(info, uid) if uid is not None else {}
    win = bool(me.get("win", True))
    ev["isWin"] = 1 if win else 0
    cleared = st.props.get("_oo_cleared_levels")
    if not isinstance(cleared, dict):
        cleared = {}
    lv_key = str(lv or 0)
    first_pass = bool(lv) and lv_key not in cleared
    if first_pass:
        ev["first_pass"] = 1
    # ★ P0-D：走事件总线（检测入口），不再直接调 story.evaluate_event；
    #   落状态仍统一走 _finish_story_tasks。
    result = _events.apply_event(
        st, _events.GameEvent(_events.EV_FINISH_DUNGEON, ev,
                              source="rpc:Cmd_Avatar_FinishGame_CS"))
    hits = result.hits
    out = []
    out.extend(drain_event_task_sync(st, result))
    if hits:
        out.extend(_finish_story_tasks(st, hits, source="event"))
    # ★ P1 阶段 G：副本打通过 -> 对应 StoryLineData.id 记到 story_finished。
    #   以前这里写死 story_finished[1]=True（只对序章），其它 16 条线永远是 false。
    #   客户端 clientavatar.lua:180 / streetstage.lua:169 / loginmanager.lua:130
    #   都读 story_finished[1]；avatarstorylinecomp.lua:105-107 按 storyLineID 读。
    # _oo_cur_copy 是 StartGame 时记下的 copyId（下面才 pop，所以这里拿得到）
    _copy = st.props.get("_oo_cur_copy")
    if not _copy and type_id == _STORY_TYPE_MAINSTORY:
        # ★ 兼容没有 StartGame 的调用（测试 / 手工构造）：
        #   序章的 typeId=1004 固定对应 StoryLineData[1]（dungeonId=guide_01）。
        #   以前就是靠这个判断写死 story_finished[1]=True 的。
        _copy = "guide_01"
    if _copy:
        _sl = story_mod.mark_story_line_finished(st, _copy)
        if _sl:
            out.extend(_sl)
            print("[dungeon] 章节线 %s 完成（copyId=%s）"
                  % ((story_mod.story_line_of_copy(_copy) or {}).get("id"),
                     _copy), flush=True)
    # ★ P0-D：链式任务也走事件总线。这里用 tick() 而不是 apply_event()，
    #   因为刚完成的任务可能让链式任务的前置条件满足了。
    tick_res = _events.tick(st)
    if tick_res.chain_hits:
        out.extend(_finish_story_tasks(st, tick_res.chain_hits, source="chain"))
    # 首通记录（下次同关卡就不是 first_pass 了）
    if lv and win:
        cleared[lv_key] = 1
        st.set("_oo_cleared_levels", cleared)
    if hits:
        print("[dungeon] 结算推进剧情 %s（事件参数 %s）" % (hits, ev),
              flush=True)
    return out

def _dungeon_finish(rid, params, uid=None):
    """Cmd_Avatar_FinishGame_CS / Cmd_OfflineSpace_Finish_CS —— 副本结算。

    1. 从 finishInfo[uid].battle_items 算奖励并发进背包
    2. 下发 AvatarSpace_SpaceFinish_SC（客户端靠它弹结算面板 + 回街区）
    3. 用 _oo_cur_match 推进剧情
    """
    vals = decode_params(params)
    info = vals[0] if (isinstance(vals, list) and vals
                      and isinstance(vals[0], dict)) else {}
    if not isinstance(info, dict):
        info = {}
    st = state(uid)
    me = _finish_info_of(info, uid)
    item_frames, reward_list = _grant_battle_items(
        st, me.get("battle_items") or {})
    syncs = list(item_frames)
    settle = _settlement_table(st, uid, info, reward_list)
    # ★ SpaceFinish_SC 是 s2c 主动推送、不是属性同步，必须留在应答【之后】。
    #   客户端日志实测顺序：sendFinishInfo -> sendFinishInfoCallback(应答)
    #   -> AvatarSettlementComp OnReceiveEndMsg(推送)。
    #   所以它走 respond_after_sync 的 tail，而不是塞进 syncs。
    pushes = [msmod.build_entity_message(
        "", "AvatarSpace_SpaceFinish_SC", RPC_AVATAR_SPACE_FINISH_SC,
        msmod.mp_arr([settle]))]
    try:
        to_street = int(settle.get("toStreetId") or 0)
    except Exception:
        to_street = 0
    if to_street and to_street != int(st.props.get("street_id") or 0):
        st.props["street_id"] = to_street
        syncs.append(sync_frame(
            SYNC_SELF, 0, msmod.OP_CHANGE, "street_id", to_street))
    syncs.extend(_advance_story_on_finish(st, info, uid))
    st.props.pop("_oo_cur_copy", None)
    st.props.pop("_oo_cur_match", None)
    st.props.pop("_oo_cur_level", None)
    st.props.pop("_oo_cur_area", None)
    st.props.pop("_oo_cur_teach", None)
    st.save()
    print("[dungeon] 结算完成：奖励 %d 项，回街区 %s 位置 %s"
          % (len(reward_list), settle.get("toStreetId"),
             settle.get("toStreetPosId")), flush=True)
    return respond_after_sync(rid, syncs, tail=pushes)

def _storyline_finish_game(rid, params, uid=None):
    """Storyline_FinishGame {storyID, passed, useTime} —— 标记剧情段落完成。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        sid = int(vals[0] or 0)
    except Exception:
        return respond_after_sync(rid, syncs)
    passed = True
    if len(vals) > 1:
        passed = bool(vals[1])
    if not passed:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    sf = st.props.get("story_finished")
    if not isinstance(sf, dict):
        return respond_after_sync(rid, syncs)
    if sf.get(sid) is not True:
        # ★ is_new 必须在赋值**之前**判断。
        #   审计发现：之前写成 `sf[sid] = True` 之后再判 `sid in sf`，
        #   恒为 True -> 新键永远发 OP_CHANGE -> 客户端 oldVal nil 报错、同步被丢。
        is_new = sid not in sf
        sf[sid] = True
        st.set("story_finished", sf)
        st.save()
        syncs.append(sync_frame(
            SYNC_SELF, sf.get("__id__") or 0,
            msmod.OP_ADD if is_new else msmod.OP_CHANGE,
            str(sid), True))
    _log.info("[dungeon] 剧情段落 %s 完成" % sid)
    return respond_after_sync(rid, syncs)
