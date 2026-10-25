#!/usr/bin/env python3
"""副本内关卡切换与战斗教学（P0-E2 第 3 步）。

★ 逆向依据 battlecontainer/dispatchhub/dispatchhubonline.lua:198-232：
  enterNextLevelCallback(..., ret, response) -> if ret.status then callback(true, response.levelInfo)
  HubPlayer:enterOfflineBattleCallback -> self.hub:initProgress(levelinfo.levelProgress)
  levelinfo 为 nil 直接抛异常（实测 HubPlayer.lua:139），下一关的 ServerBattle
  根本建不起来，角色在空场景里一直坠落。所以应答必须带 levelInfo。

BattleTeach_Activate_CS 写 battle_teach_state[iid] = 0 并推同步：
  battle_main_panel.lua:513-516  if newVal ~= 0 then return end  => 0 才弹教学 UI
"""
import time

from shared.log_tee import get_logger

from .. import gamedata
from .. import ms as msmod
from ..rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame
from ..rpc_index import ACK_OK
from .context import _remember_dungeon

_log = get_logger(__name__)
def _avatar_enter_next(rid, params, uid=None):
    """Cmd_Avatar_EnterNext_CS {copyIndex} —— 回下一关的 levelInfo。

    客户端 hubplayer.lua:116-121 会读：
      levelinfo.levelProgress / towerLevelInfo / randSeed / runTime
    少了就是 nil 索引异常，下一关建不起来。
    """
    vals = decode_params(params)
    copy_index = 0
    if isinstance(vals, list) and vals:
        try:
            copy_index = int(vals[0] or 0)
        except Exception:
            copy_index = 0
    st = state(uid)
    level_info = {
        "levelProgress": {},
        "randSeed": int(time.time()) & 0x7FFFFFFF,
        "runTime": 0,
    }
    # ★ 切到下一关时更新「当前关卡」，结算时要用它算 levelId / areaid
    copy_id = st.props.get("_oo_cur_copy")
    roots = (gamedata.level_copy(copy_id) or {}).get("rootLevel") or []
    if 1 <= copy_index <= len(roots):
        _remember_dungeon(st, copy_id, roots[copy_index - 1])
        st.save()
        print("[dungeon] 进入下一关 copyIndex={} 关卡={}".format(copy_index, st.props.get("_oo_cur_level")), flush=True)
    else:
        # 越界不算错（客户端可能重发/乱序），但**不能改上下文**，也不能假装进了关。
        # 以前这行日志写在 if 外面，越界时也会打「进入下一关 关卡=旧值」，误导排障。
        print("[dungeon] 忽略越界的 copyIndex={}（本副本 {} 关，当前关卡保持 {}）".format(copy_index, len(roots), st.props.get("_oo_cur_level")),
              flush=True)
    return [msmod.build_entity_response(
        rid, ret=dict(ACK_OK),
        resp={"levelInfo": level_info})]

def _battle_teach_activate(rid, params, uid=None):
    """BattleTeach_Activate_CS {instructionID} —— 激活战斗教学。

    客户端 avatarsystemguidecomp.lua:406-408 -> serverRpc("BattleTeach_Activate_CS")
    服务端写 battle_teach_state[instructionID] = 0 并推同步：
      config.lua:10084 AvatarSystemGuideComp
        entryAdded.battle_teach_state -> onAddBattleTeach
        changed["battle_teach_state.*"] -> onChangedBattleTeachState
      battle_main_panel.lua:513-516  if newVal ~= 0 then return end
      => 值为 0 才会弹教学 UI
    """
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, syncs)
    try:
        iid = int(vals[0] or 0)
    except Exception:
        return respond_after_sync(rid, syncs)
    st = state(uid)
    # ★ 2026-10-23 实机日志实锤：防客户端崩。
    #   序章技能节点 ActivateBattleTeach 的 instructionID=312，
    #   但 InstructionData 表没有 312（原始表 45 条，300-314 缺 312）。
    #   照常写 battle_teach_state[312]=0 并推同步的话，客户端
    #   onChangedBattleTeachState 读 InstructionData[312]=nil ->
    #   Battle_Main_Panel.lua:625 "attempt to index local instructionData"
    #   -> 弹「神秘力量」。表里没有的教学直接拒绝（回 ACK + 日志），
    #   客户端不会收到这条同步，就不会崩。
    if gamedata.table("InstructionData").get(str(iid)) is None:
        print("[dungeon] 教学 {} 不在 InstructionData 表，拒绝激活（防客户端崩）".format(iid), flush=True)
        return respond_after_sync(rid, syncs)

    bts = st.props.get("battle_teach_state")
    if not isinstance(bts, dict):
        return respond_after_sync(rid, syncs)
    # 记下本次副本激活过的教学指令 —— 结算时喂 instructionid（6 个事件需要）
    st.set("_oo_cur_teach", iid)
    if iid not in bts:
        bts[iid] = 0
        st.set("battle_teach_state", bts)
        st.save()
        syncs.append(sync_frame(
            SYNC_SELF, bts.get("__id__") or 0, msmod.OP_ADD, str(iid), 0))
        _log.info("[dungeon] 激活战斗教学 {}".format(iid))
    return respond_after_sync(rid, syncs)
