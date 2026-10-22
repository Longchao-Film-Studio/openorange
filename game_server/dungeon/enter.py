#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进副本入口（P0-E2 第 3 步）。

两条入口：
  Cmd_Avatar_StartGame_CS  序章/剧情副本（应答要带 spaceInfo 嵌套）
  SingleStartMatch_CS      普通副本（主线/挑战/塔…，应答只读 status）

★ SingleStartMatch_CS 之前完全没接，所以除序章外 321 个副本进不去（审计发现）。
★ Cmd_Avatar_StartGame_CS 的应答里 spaceInfo 要再套一层 spaceInfo：
  客户端 SpaceUtils.enterOnlineHub 读的是 data.spaceInfo.copyId。
"""
from .. import config as ooconf
from .. import gamedata
from .. import ms as msmod
from ..rpc_common import decode_params, state
from ..rpc_index import ACK_OK
from .context import _STORY_TYPE_MAINSTORY, _remember_dungeon
def _resolve_copy_id(match_info):
    """按 match_info 解析出 Level_CopyData 的 copyId。"""
    if not isinstance(match_info, dict):
        return None
    st = match_info.get("system_type")
    # MainStory: StoryLineData[storyID].dungeonId
    if st == _STORY_TYPE_MAINSTORY:
        sid = match_info.get("storyID") or match_info.get("storyId")
        line = gamedata.story_line(sid)
        if isinstance(line, dict) and line.get("dungeonId"):
            return line["dungeonId"]
        return None
    # 主线副本（system_type == Mainline）：match_info.dungeonId 是 MainlineData 的
    # **整数 ID**，copyId 要从表里取 dungeonid。
    # 依据：客户端 AvatarMatch:GetSpaceLevel（avatarmatch.lua:29-31）
    #   local config = DataLoader:getMainlineData()[match_info.dungeonId]
    #   return config.dungeonid
    did = match_info.get("dungeonId")
    if isinstance(did, int) or (isinstance(did, str) and did.isdigit()):
        ml = gamedata.mainline(did)
        if isinstance(ml, dict) and ml.get("dungeonid"):
            return ml["dungeonid"]
    # 其它系统：先看调用方有没有直接给 copyId / dungeonId（字符串形式）
    #
    # ★ P1 阶段 N（审计 P0-2 修复）：必须校验这个 copyId 真的存在。
    #   以前直接返回客户端给的任意字符串 —— 攻击者给个不存在的 copyId 就能
    #   把 _oo_cur_copy 设成任意值，再配合 FinishSpaceTask_Directly_CS 完成
    #   任意副本的 type0（L2 的副本校验形同虚设）。
    for k in ("copyId", "dungeonid", "dungeonId"):
        v = match_info.get(k)
        if isinstance(v, str) and v and not v.isdigit():
            if gamedata.level_copy(v):
                return v
            print("[dungeon] 拒绝：copyId=%s 不在 Level_CopyData 里" % v,
                  flush=True)
            return None
    return None

def _avatar_start_game(rid, params, uid=None):
    """Cmd_Avatar_StartGame_CS {match_info} —— 副本入口，回 spaceInfo。"""
    st = state(uid)
    vals = decode_params(params)
    match_info = vals[0] if (isinstance(vals, list) and vals
                             and isinstance(vals[0], dict)) else {}
    if not isinstance(match_info, dict):
        match_info = {}
    copy_id = _resolve_copy_id(match_info)
    if not copy_id:
        print("[dungeon] 解析不出 copyId，match_info=%s" % match_info, flush=True)
        return [msmod.build_entity_response(
            rid, ret={"status": False, "errorcode": 1})]
    copy_cfg = gamedata.level_copy(copy_id) or {}
    roots = copy_cfg.get("rootLevel") or []
    # ★ 注意嵌套：客户端 SpaceUtils.enterOnlineHub(stage, data, offlineSpace)
    #   读的是 data.spaceInfo.copyId / data.spaceInfo.levelIndex，
    #   而 Storyline_StartGame_Callback 传进去的是 resp.spaceInfo。
    #   所以 RPC 应答的 spaceInfo 字段里还要再套一层 spaceInfo。
    #   踩过：只给一层时客户端报
    #     SpaceUtils.lua:226 attempt to index field 'spaceInfo' (a nil value)
    inner = {
        "copyId": copy_id,
        "levelIndex": 1,          # getCopy 是 1-based
        "systemType": match_info.get("system_type"),
        "match_info": match_info,
    }
    if roots:
        ids = [int(x) for x in roots if isinstance(x, int)]
        # 客户端 dispatchhubonline.lua:49-59 优先用服务端给的 levelIds，
        # 所以覆盖关卡列表就能直接进指定关卡（跳过前面的关卡）。
        # ★ 按账号的关卡覆盖（存玩家存档的 _oo_ 私有字段，客户端看不到）。
        #   之前是全局的，多开/换号时所有人都会被塞进同一关 —— 那是错的。
        force = 0
        try:
            force = int(st.props.get("_oo_dungeon_force_level") or 0)
        except (TypeError, ValueError):
            force = 0
        if force:
            if force in ids:
                ids = [force]
                print("[dungeon] 强制关卡 %s（%s 的账号设置）" % (force, uid),
                      flush=True)
            else:
                print("[dungeon] ⚠ 强制关卡 %s 不属于本副本（%s），忽略"
                      % (force, ids), flush=True)
        elif ooconf.DUNGEON_LAST_ONLY and len(ids) > 1:
            # 命令行 --dungeon-last-only：只下发最后一关
            ids = ids[-1:]
            print("[dungeon] --dungeon-last-only：只下发最后一关 %s" % ids,
                  flush=True)
        inner["levelIds"] = ids
    space_info = {"spaceInfo": inner, "battleInfo": {}}
    st.props["_oo_cur_copy"] = copy_id
    # 结算时要用它反查 typeId/modeId 推进剧情（客户端 finishInfo 里没有）
    st.props["_oo_cur_match"] = match_info
    # ★ 结算时喂给剧情引擎的额外事件参数：FinishDungeon 事件里有 57 个带
    #   areaid、4 个带 levelId，不给就永远匹配不上（event_matches 要求全字段）。
    _remember_dungeon(st, copy_id, (inner.get("levelIds") or [None])[0])
    st.props.pop("_oo_cur_teach", None)   # 新副本，清掉上一轮的教学记录
    st.save()
    print("[dungeon] 进副本 copyId=%s roots=%s" % (copy_id, roots), flush=True)
    resp = {"status": True, "spaceInfo": space_info}
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK), resp=resp)]

def _single_start_match(rid, params, uid=None):
    """SingleStartMatch_CS {match_info} —— 普通副本入口（主线/挑战/塔…）。

    ★ 之前完全没接，所以除序章外 321 个副本进不去（审计发现）。

    客户端 avatarteamcomp.lua:269-315 发这个 RPC；回调 SignleStartMatch_SC
    （:321-331）**只读 ret.status / ret.errorcode**，copyId 是客户端自己算的：
      local copyId = self:GetSpaceLevel(match_info)   -- AvatarMatch:GetSpaceLevel
      SpaceUtils.showLoadingPanel(EGameStage.OfflineBattle, copyId, false)
    所以服务端只需要两件事：
      1. 记下本次副本上下文（结算时要用）
      2. 回 {status: true}

    match_info 的 shape 见 avatarteamcomp.lua:271-312，关键字段：
      system_type / modeId / dungeonId / levelId / storyID / match_type ...
    system_type == Mainline 时 dungeonId 是 MainlineData 的整数 ID。
    """
    frames = []
    st = state(uid)
    vals = decode_params(params)
    match_info = vals[0] if (isinstance(vals, list) and vals
                             and isinstance(vals[0], dict)) else {}
    if not isinstance(match_info, dict):
        match_info = {}
    copy_id = _resolve_copy_id(match_info)
    if not copy_id:
        print("[dungeon] SingleStartMatch 解析不出 copyId，match_info=%s"
              % match_info, flush=True)
        return [msmod.build_entity_response(
            rid, ret={"status": False, "errorcode": 1})]
    cfg = gamedata.level_copy(copy_id) or {}
    roots = cfg.get("rootLevel") or []
    st.props["_oo_cur_copy"] = copy_id
    st.props["_oo_cur_match"] = match_info
    # 客户端请求里带了具体关卡就用它，否则用副本首关
    # ★ P1 阶段 N（审计 P0-2 修复）：levelId 必须是该副本 rootLevel 里的一个。
    #   以前直接信任客户端给的整数，攻击者可以填任意值把 _oo_cur_level 设成
    #   某个 SpaceTask 要求的关卡，从而在没进战斗的情况下完成 type0。
    want_level = match_info.get("levelId")
    root_ints = [int(x) for x in roots if isinstance(x, int)]
    if not isinstance(want_level, int):
        want_level = root_ints[0] if root_ints else None
    elif root_ints and int(want_level) not in root_ints:
        print("[dungeon] 拒绝：levelId=%s 不在 %s 的 rootLevel %s 里，改用首关"
              % (want_level, copy_id, root_ints[:4]), flush=True)
        want_level = root_ints[0]
    _remember_dungeon(st, copy_id, want_level)
    st.props.pop("_oo_cur_teach", None)
    st.save()
    print("[dungeon] SingleStartMatch copyId=%s level=%s area=%s"
          % (copy_id, st.props.get("_oo_cur_level"),
             st.props.get("_oo_cur_area")), flush=True)
    # 客户端只读 status，不需要 spaceInfo
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK),
                                        resp={"status": True})]
