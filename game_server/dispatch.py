#!/usr/bin/env python3
"""实体 RPC（serverRpc）应答层。

为什么需要这一层
----------------
客户端 entities/avatarcomponent/avatarrpccomp.lua:17-50 的 _sendServerRpc：

    local turnCircleTime = 2
    local disposable = self:addTimer(turnCircleTime, function()
      ui.postEvent(EUIEventDefine.EStartWeakNetTurnCircle)
    end)
    ...
    self.server:serverRpc(name, parameters, responseCB)

也就是说 **任何一个 serverRpc 只要 2 秒内没收到应答，客户端就弹弱网转圈，
接着 DisconnectionPanel 走重新登录**。所以服务端必须对每条带 rid 的实体
RPC 都回一个 entityMessageResponse。

应答长什么样
------------
core/client/gateclientrpchandler.lua:128-150：

    function GateClientRpcHandler:entityMessageResponse(rid, parameters)
      local name, callback = ClientRepo.responseManager:popResponseCallback(rid)
      if callback then
        local retStatus, response = unpack(parameters)
        SafeCallback(callback, retStatus, response)
      end
    end

所以 parameters 是 [retStatus, response] 两元组，帧类型 3。
retStatus 只需要有 status 字段 —— 三个组件里所有 _SC 处理函数第一句都是
if retstatus.status then ...（avatarbagcomp / avatarcharactercomp /
avataruserdatacomp 逐个确认过），只有极少数还读 errorcode / color。

索引来源
--------
分层（P0-E2 拆完后的样子）
--------------------------
    entity_rpc.py      兼容层（历史名字），只 re-export
    dispatch.py        本文件：装配层，只做「索引 -> 处理函数」和 handle()
    rpc_index.py       索引表 + 应答常量
    rpc_common.py      状态 / 帧构造 / 节流存档 / 参数解码
    property/          Set* 系列、背包、角色、Tx/sync/ids/spec
    story/             剧情状态机、事件总线、剧情/通用任务 RPC
    space/             空间、移动、伊甸、Soul
    dungeon/           进本、关卡、结算、发奖
    street/            街区切换、阴阳界、交互、宝箱、声望、派遣
    misc/              无归属的单向消息、表情、登录查询

索引来源
--------
名字 -> 索引 全部来自 tools/hash_rpc_names.py（genRpcIndex 哈希预言机），
可用 hash_rpc_names.py verify 重新核对，不靠记忆。
"""
from . import config as _ooconf
from . import ms as msmod
from . import rpc_catalog as _catalog
from . import unknown_log as _ulog

# 跨业务域共用的基础设施（P0-E2）：状态 / 帧构造 / 节流存档 / 参数解码
from .rpc_common import (  # noqa: F401  (re-export)
    _LAST_SAVE,
    _SAVE_INTERVAL,
    EVENT_MAX_DEPTH,
    decode_params,
)
from .rpc_common import (
    respond_after_sync as _respond_after_sync,
)
from .rpc_common import (
    state as _state,
)
from .rpc_common import (  # noqa: F401  (re-export -> entity_rpc)
    sync_frame as _sync_frame,
)
from .rpc_common import (  # noqa: F401  (re-export -> entity_rpc)
    throttled_save as _throttled_save,
)

# 名字 -> genRpcIndex 的索引表已拆到 rpc_index.py（P0-E1）；
# 这里 re-export，保持 erpc.RPC_INDEX / erpc.describe 等既有调用点不变。
from .rpc_index import (  # noqa: F401  (re-export)
    ACK_OK,
    ONE_WAY,
    RPC_INDEX,
    describe,
    name_of,
)

# 兼容旧名：剧情任务递归深度上限
_EVENT_MAX_DEPTH = EVENT_MAX_DEPTH

# 业务域模块（P0-E2 第 1 步）：Set* 系列 / 无状态 RPC / 表情
from shared.log_tee import get_logger

# ★ P1 阶段 N-a1：取名链路（之前 3 个 RPC 全缺，见 game_server/avatar.py 的说明）
from .avatar import (  # noqa: F401  (re-export)
    _change_nickname,
    _check_nickname,
    _commit_name_gender,
)

# 业务域模块（P0-E2 第 3 步）：dungeon / 背包 / 角色 / 登录查询
from .dungeon.context import _STORY_TYPE_MAINSTORY, _remember_dungeon  # noqa: F401
from .dungeon.enter import (  # noqa: F401  (re-export)
    _avatar_start_game,
    _resolve_copy_id,
    _single_start_match,
)
from .dungeon.level import (  # noqa: F401  (re-export)
    _avatar_enter_next,
    _battle_teach_activate,
)
from .dungeon.reward import _grant_battle_items  # noqa: F401
from .dungeon.settle import (  # noqa: F401  (re-export)
    RPC_AVATAR_SPACE_FINISH_SC,
    _advance_story_on_finish,
    _dungeon_finish,
    _finish_info_of,
    _settlement_table,
    _storyline_finish_game,
)
from .misc.emoji import _play_act_emoji  # noqa: F401
from .misc.session import _match_duration, _sunact_level, _team_info  # noqa: F401
from .misc.unimplemented import _ack_only, _noop_one_way  # noqa: F401
from .npc_chat import (  # noqa: F401  (re-export)
    _check_topic_can_open,
    _gm_open_npc_chat_topic,
    _update_npc_chat_info,
)
from .property.character import _upgrade_character  # noqa: F401
from .property.items import (  # noqa: F401  (re-export)
    _USE_PARAM_PROP,
    _apply_item_effect,
    _record_item_obtain,
    _submit_item,
    _use_item,
)
from .property.setters import (  # noqa: F401  (re-export)
    _CONST_SETTERS,
    _SIMPLE_SETTERS,
    _make_setter,
    _set_character_list,
    _set_character_list_name,
    _set_signature,
)
from .space.eden import _eden_chat_history, _enter_eden  # noqa: F401
from .space.move import _change_micro_state, _client_move  # noqa: F401
from .space.soul import (  # noqa: F401  (re-export)
    CMD_PROPERTY_RELIABLE_SYNC_SC,
    SOUL_HEARTBEAT_SC,
    SYNC_FRAME_FROM_SERVER,
    _reliable_property_sync,
    _soul_heartbeat,
    _space_frame_message,
)

# 业务域模块（P0-E2 第 2 步）：space / story
from .space.space import (  # noqa: F401  (re-export)
    RPC_ON_ENTER_SPACE,
    RPC_ON_TELEPORT_OUT,
    SPACE_EDEN,
    _enter_space,
    _leave_space_rpc,
    _s2c_entity,
    _scene_loaded,
    _space_prop,
    _space_sync_frames,
)
from .story.rpc import (  # noqa: F401  (re-export)
    _done_story_task,
    _emit_get_item,
    _find_story_tasks_by_param,
    _finish_space_task,
    _finish_story_tasks,
    _recalc_story_unlocks,
    _story_sync_frames,
    _street_actor,
)
from .story.tasks import (  # noqa: F401  (re-export)
    _done_title_task,
    _refresh_title_daily_task,
    _task_done,
    _task_done_type,
    _task_fetch,
    _task_sync_frames,
)
from .street.dispatch import (  # noqa: F401  (re-export)
    _cancel_dispatch,
    _dispatch_char,
    _finish_dispatch,
)
from .street.enter import (  # noqa: F401  (re-export)
    _apply_street_change,
    _leave_street,
    _switch_street,
    _update_street_pos,
)

# 业务域模块（P0-E2 第 4 步）：street
from .street.info import (  # noqa: F401  (re-export)
    _make_street_info_entry,
    _street_info_of,
    _street_tx_begin,
)
from .street.interact import (  # noqa: F401  (re-export)
    _actor_interact,
    _clear_treasure_actor,
    _street_redpoint,
    _street_trace_task,
)
from .street.misc import (  # noqa: F401  (re-export)
    _finish_storyline,
    _mainline_levelup,
    _street_cd_update,
)
from .street.prestige import (  # noqa: F401  (re-export)
    _add_prestige_exp,
    _apply_prestige_exp,
    _get_prestige_award,
    _get_prestige_task_award,
    _prestige_info,
    _prestige_push,
)
from .street.surface import (  # noqa: F401  (re-export)
    _enter_surface,
    _leave_surface,
    _set_surface_time,
)
from .street.treasure import (  # noqa: F401  (re-export)
    _grant_treasure_reward,
    _street_treasure_rpc,
    _touch_treasure,
)

_log = get_logger(__name__)

_HANDLERS = {
    msmod.RPC_INDEX_SET_SIGNATURE_CS: _set_signature,
    msmod.RPC_INDEX_USE_ITEM_CS: _use_item,
    msmod.RPC_INDEX_UPGRADE_CHARACTER_CS: _upgrade_character,
    msmod.RPC_GET_SUNACT_LEVEL_CS: _sunact_level,
    msmod.RPC_GET_MATCH_DURATION: _match_duration,
    msmod.RPC_QUERY_MY_TEAM_INFO_CS: _team_info,
}

# 把 1-3 的 Set* 系列挂上（真正改数据 + 推同步，不再是假 ACK）
for _name, _spec in _SIMPLE_SETTERS.items():
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _make_setter(_spec, False)
for _name, _spec in _CONST_SETTERS.items():
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _make_setter(_spec, True)

# 编队内容（改的是 character_list 的子属性，不是根属性，单独处理）
for _name, _fn in (("SubmitItem_CS", _submit_item),
                   ("RecordItemObtain_CS", _record_item_obtain),
                   ("SetCharacterList_CS", _set_character_list),
                   ("SetCharacterListName_CS", _set_character_list_name),
                   ("RequestSwitchStreet_CS", _switch_street),
                   ("RpcEnterStreet", _switch_street),
                   ("RequestLeaveStreet_CS", _leave_street),
                   ("RequestDoneStoryTaskParamInt_CS", _done_story_task),
                   ("RequestDoneStoryTaskParamStr_CS", _done_story_task),
                   ("RequestStreetActor_CS", _street_actor),
                   ("RequestFetchTask_CS", _task_fetch),
                   ("RequestDoneTask_CS", _task_done),
                   ("RequestCompleteTask_CS", _task_done),
                   ("RequestCompleteUnitlTask_CS", _task_done),
                   ("RequestDoneSomeTypeTask_CS", _task_done_type),
                   ("RpcLeaveSpace", _leave_space_rpc),
                   ("Street_Actor_InteractID_Update_CS", _actor_interact),
                   ("Street_Redpoint_Update_CS", _street_redpoint),
                   ("updateStreetTraceTask_CS", _street_trace_task),
                   ("RequestEnterStreetSurface_CS", _enter_surface),
                   ("RequestStreetTreasure_CS", _street_treasure_rpc),
                   ("Street_Mainline_Levelup_CS", _mainline_levelup),
                   ("finishStreetStoryline_CS", _finish_storyline),
                   ("Check_TopicID_CanOpen_CS", _check_topic_can_open),
                   ("Update_NpcChat_Info_CS", _update_npc_chat_info),
                   ("GmRpcOpenNpcChatTopic", _gm_open_npc_chat_topic),
                   ("RpcGetEdenChatHistory", _eden_chat_history),
                   ("RequestAddPrestigeExp_CS", _add_prestige_exp),
                   ("RequestGetPrestigeAward_CS", _get_prestige_award),
                   ("RequestGetPrestigeTaskAward_CS", _get_prestige_task_award),
                   ("RequestDispatchChar_CS", _dispatch_char),
                   ("RequestFinishDispatch_CS", _finish_dispatch),
                   ("RequestCancelDispatchChar_CS", _cancel_dispatch),
                   ("RequestUpgradeDungeonMainPower_CS", _ack_only),
                   ("RequestGetStreetLevelAchieveAward_CS", _ack_only),
                   ("Street_CD_Update_CS", _street_cd_update),
                   ("RequestRefreshTitleDailyTask_CS", _refresh_title_daily_task),
                   ("RequestDoneTitleTask_CS", _done_title_task),
                   ("RollbackSpaceTask_CS", _ack_only),
                   # ★ P1 阶段 N-a1：取名（namecomp.lua:7-36）
                   ("RpcCheckNickName", _check_nickname),
                   ("RpcChangeNickName", _change_nickname),
                   ("RpcCommitNameGender", _commit_name_gender),
                   # ★ P1 阶段 J：战斗内 SpaceTask 完成 -> 剧情任务（type0 的唯一完成路径）
                   ("FinishSpaceTask_Directly_CS", _finish_space_task),
                   ("Cmd_OfflineSpace_Finish_CS", _dungeon_finish),
                   ("Cmd_Dungeon_FinishInteract_CS", _ack_only),
                   ("Cmd_Infinite_FinishInteract_CS", _ack_only),
                   ("Cmd_Avatar_StartGame_CS", _avatar_start_game),
                   ("Storyline_FinishGame", _storyline_finish_game),
                   ("Cmd_Avatar_EnterNext_CS", _avatar_enter_next),
                   ("Cmd_Avatar_FinishGame_CS", _dungeon_finish),
                   ("SingleStartMatch_CS", _single_start_match),
                   ("BattleTeach_Activate_CS", _battle_teach_activate),
                   ("RecordItemObtain_CS", _record_item_obtain)):
    if _name in RPC_INDEX:
        _HANDLERS[RPC_INDEX[_name]] = _fn

# ---------------------------------------------------------------------------
# 单向消息处理器（serverMsg / CallServer，rid = 0）
# ---------------------------------------------------------------------------
# 这些不回应答，只改服务端数据。

_ONE_WAY_HANDLERS = {
    294469585: _update_street_pos,   # RequestUpdateStreetPos_CS
    1277075552: _enter_space,        # RpcEnterSpace
    996547331: _scene_loaded,        # RpcOnSceneLoaded
    1588890197: _enter_eden,         # RpcEnterEden
    1369842456: _enter_eden,         # RpcBackToEden
    1920167376: _client_move,        # RpcOnClientMove（AOI）
    1517096639: _change_micro_state, # RpcChangeMicroState
    1675033651: _clear_treasure_actor,  # RequestClearStreetTreasureActor_CS
    2137482665: _leave_surface,         # RequestLeaveStreetSurface_CS
    1282426820: _touch_treasure,        # RequestTouchTreasure_CS
    2000707256: _play_act_emoji,        # RpcPlayActEmoji（AOI）
    2099437863: _noop_one_way,          # RpcEnterTargetSeq
    2031265256: _noop_one_way,          # Cmd_Avatar_StatEnterBattleFialed_CS
    790742624: _noop_one_way,           # Cmd_LoadingProcessSync_CS
    # --- Soul / 战斗（Phase 3 才真正跑起来，先把面补全） ---
    1768525703: _soul_heartbeat,        # soulHeartbeat_CS
    1136123026: _reliable_property_sync, # Cmd_Property_ReliableSync_CS
    1640580279: _space_frame_message,   # spaceFrameMessage
    577431259: _noop_one_way,           # spaceRpcMessage
    780255370: _noop_one_way,           # defineRpcMessageToLogic
    670252998: _noop_one_way,           # sendDungeonMsgToServer
    1740981687: _noop_one_way,          # Space_CallSpaceMethod_CS
    1909403499: _noop_one_way,          # continueSendMsg_CS
    1062918872: _noop_one_way,          # RpcSoulAckReliableMsg
    531534451: _noop_one_way,           # Cmd_Soul_WatchOtherPlayer_CS
    1936154067: _noop_one_way,          # sendDDZMessage
    739755462: _noop_one_way,           # sendMahjongMessage
    649611053: _noop_one_way,           # earlyLeaveDDZ
    404472060: _noop_one_way,           # earlyLeaveMahjong
}

def _drain_pending_chain(uid=None):
    """把登录时事件总线攒下的「待完成链式任务」补完，返回同步帧。

    为什么要攒：ms._live_init_dict 里跑 events.tick() 时还不知道要怎么发奖、
    怎么推同步（那是 entity_rpc 的事）。所以只把命中的任务 id 记在
    _oo_pending_chain 里，等第一条 RPC 进来时在这里补完。

    ★ 2026-10-23 优化：不再**每条 RPC 都跑**。用 player_state 的 per-uid
      标记「本登录已补完」，登录（_live_init_dict）时 reset，首条 RPC 后 mark，
      之后直接跳过 _state()+pop。
    """
    try:
        from .. import player_state as pstate
    except Exception:
        pstate = None
    if pstate is not None and pstate.drain_done(uid):
        return []
    try:
        st = _state(uid)
    except Exception:
        return []
    if pstate is not None:
        # 本轮登录已经取到过状态：无论有没有 pending，都算「补完过」，
        # 后续 RPC 不再重复查。pending 为空则无事可做，标记照常。
        pstate.mark_drain(uid)
    pending = st.props.pop("_oo_pending_chain", None)
    if not pending:
        return []
    try:
        # ★ P1 阶段 N：`_oo_pending_chain` 里混了两种来源 ——
        #   登录 tick 的**事件命中**（type3/4/7）和**链式命中**（type6）。
        #   以前统一按 chain 传，白名单会把 type3/4/7 全挡掉
        #   （回归实测：400015 是 type3，永远完不成）。
        from . import gamedata as _gd
        _ids = [int(x) for x in pending]
        _ev, _ch = [], []
        for _t in _ids:
            _tt = (_gd.story_task(_t) or {}).get("TaskType")
            if _tt == 6:
                _ch.append(_t)
            else:
                _ev.append(_t)
        _out = []
        if _ev:
            _out.extend(_finish_story_tasks(st, _ev, source="event"))
        if _ch:
            _out.extend(_finish_story_tasks(st, _ch, source="chain"))
        return _out
    except Exception as e:
        _log.warning(f"[story] 补完成链式任务失败: {e}")
        return []

# ---------------------------------------------------------------- 未实现 RPC（雷2）
# ★ 2026-10-23：以前未实现的 RPC 一律回 {status:true}（"通用 ACK"）。
#   后果：客户端以为成功、服务端什么都没做 —— 邮件附件被本地标记已领、
#   奖励永久丢失就是这条路。现在改成：
#     - 严格模式（默认）：回失败，把缺口暴露出来
#     - 兼容模式（--unknown-ok / OO_UNKNOWN_OK=1）：回成功，避免弱网重连风暴
#   两种模式都记到 work/logs/unknown_requests.tsv（含聚合计数）。
#
#   _NEVER_FAKE_OK_RPC 是**硬黑名单**：即使兼容模式也绝不回成功。
#   判据：客户端收到 status=true 后会本地改状态、或显示"已获得/已购买"。
_NEVER_FAKE_OK_RPC = frozenset((
    # 经济类：未实现却回成功会让 UI 显示买到/抽到（既不发货也不扣费）
    "RpcBuyShop", "RpcDrawGacha", "RpcDrawNonReplacementGacha",
    "RpcDrawPupilGacha", "RpcDrawAccuChargeGacha", "RpcObtainAccuGachaReward",
    "RpcGainAccuChargeGachaToken", "RpcGetGachaNormalReward",
    "RpcGetGachaUpOnlyReward", "RpcSetGachaWish",
    "Charge_FirstChargeReward_CS", "Cmd_BuyBpLevel_CS",
    "Cmd_AmusementCoin_Buy_CS", "RpcChargeRebateReward",
    "RpcCountdownGiftActBuy", "RpcCycleChargeActReward",
    "RpcGainPhysicalPowerBpActPayBpLevelReward",
    "RpcGainReturningPlayerActPayBpLevelReward",
))

# 严格模式下的失败应答（客户端 _SC 回调第一句都是 if retstatus.status then ...）
UNKNOWN_FAIL_RET = {"status": False, "errmsg": "not implemented"}


def unknown_rpc_ret(index, uid=None, params=None):
    """未实现 RPC 的应答 ret：记日志 + 按模式决定成败。"""
    name = _catalog.name_of(index)
    key = name or (f"index:{index}")
    fake = _ooconf.unknown_fake_ok() and (name not in _NEVER_FAKE_OK_RPC)
    _ulog.record("rpc", key, uid=uid, params=params, fake_ok=fake,
                 reply="ack" if fake else "fail")
    return dict(ACK_OK) if fake else dict(UNKNOWN_FAIL_RET)


def handle(index, params, rid, uid=None):
    """处理一条实体 RPC，返回**帧列表**（可能为空）。

    uid：这条连接对应的账号 uid（多账号时由服务端从 gate 连接上带过来）。

    返回列表是因为有的 RPC 除了应答帧，还要推额外的帧（比如 SetSignature_CS
    改完签名要再推一条 propertySync2Client）。

    空列表表示这条不用应答：
      - index 为 None；
      - rid <= 0 的单向消息（serverMsg / CallServer），回了客户端反而会报
        "entityMessageResponse got rid 0"（gateclientrpchandler.lua:138）。
    """
    if index is None:
        return []
    # ★ 雷5：同一 uid 的所有 RPC 串行执行（心跳/登录初始化也走同一把锁）。
    #   锁内只改状态 + 构造帧；发送在 gate 里做，不在锁内。
    from . import player_state as _pstate
    with _pstate.lock_for(uid):
        return _handle_locked(index, params, rid, uid)


def _handle_locked(index, params, rid, uid=None):
    # ★ 单向消息（serverMsg / CallServer，rid = 0）以前在这里直接 return []，
    #   处理器根本没被调用 —— 但服务端**必须**处理它们（位置上报、红点、
    #   清理等全是单向的），只是不能回应答帧。
    #   所以单向消息走 _ONE_WAY_HANDLERS，只改数据、不回包。
    # ★ P0-D：把登录时事件总线攒下的「待完成剧情任务」补上。
    #   这是「服务端主动推进」的落地点：不依赖任何具体 RPC，
    #   登录后第一条消息（**包括单向消息**）就顺带把漏掉的剧情推进完。
    #   审计 P1-7：之前放在 rid<=0 之后，单向消息不会 drain，会一直滞留。
    _pending_frames = _drain_pending_chain(uid)
    if rid <= 0:
        fn1 = _ONE_WAY_HANDLERS.get(index)
        if fn1 is not None:
            try:
                out = fn1(params, uid)
            except Exception as e:
                _log.warning(f"[rpc] 单向处理失败 index={index}: {e}")
                out = None
            # 单向处理器可以返回「服务端主动下发的 S2C 帧」（比如 RpcOnEnterSpace）
            if isinstance(out, list):
                return _pending_frames + out
        return _pending_frames
    fn = _HANDLERS.get(index)
    if fn is not None:
        out = fn(rid, params, uid)
        if out is None:
            return _pending_frames
        frames = out if isinstance(out, list) else [out]
        return _pending_frames + frames
    # ★ 雷2：未实现的 RPC —— 记日志 + 按模式回成功/失败（含硬黑名单）。
    # _pending_frames 是登录补完剧情的同步帧（必须在应答前），走统一入口保证顺序。
    _ret = unknown_rpc_ret(index, uid=uid, params=params)
    return _respond_after_sync(rid, _pending_frames, ret=_ret)
