#!/usr/bin/env python3
"""game_server.avatar —— 玩家资料类 RPC（取名 / 改名）。

逆向依据 `entities/avatarcomponent/namecomp.lua`：

```lua
  :7-9    CheckNickName(name, cb)      -> serverRpc("RpcCheckNickName", {name})
  :20-22  ChangeNickName(name, cb)     -> serverRpc("RpcChangeNickName", {name})
  :33-36  CommitNameGender(name, gender, cb)
                                       -> serverRpc("RpcCommitNameGender", {name, gender})
```

取名面板调用点：`ui/panel/creatingrolepanel/creatingrolepanel.lua:95`
→ `GlobalData.Avatar:CommitNameGender(self.signName, self.gender, cb)`

★ 为什么必须实现（P1 阶段 N-a1，2026-10-22 取证发现）

这 3 个 RPC 之前**全缺**，造成一条完整断链：

```
取名面板 -> RpcCommitNameGender（未实现，静默失败）
  -> player_name 永远是默认值 "未具名导演"
  -> TaskEventData[1000] 的 Sign 事件从不发出
  -> TaskData[10]「完成取名」永不完成（TriggerEventList=[1000]）
  -> TaskData[1]「完成序章」永不进包（PreTask=10）
  -> task_bag[1][1] 永不存在
  -> getSystemTaskState(1) 恒返回 -1
  -> AllSystemCatalogueData 里 40 条 unlockType=1 的系统入口数据条件永假
```

错误码（`common/systemconst.lua` 的 `NameErrorCode`）：

| 值 | 名字 | 含义 |
|---|---|---|
| 8001 | `WordCheck_OK` | 通过 |
| 8002 | `WordCheck_NameInVaild` | 名字含非法字符 |
| 8003 | `WordCheck_NameAlreadyExisted` | 重名 |
| 8004 | `WordCheck_NameLengthInVaild` | 长度不合法 |
| 8005 | `WordCheck_Exception` | 异常 |
| 8007 | `WordCheck_ChangePlayerNameInCD` | 改名冷却中 |

客户端 `checkNameResult(ret)`（namecomp.lua:46-67）的判定顺序：
`ret.status` 为真即成功；否则看 `ret.errorcode`。所以成功时回 `{status: True}` 就够。
"""
from shared.log_tee import get_logger

from . import ms as msmod
from .rpc_common import SYNC_SELF, decode_params, respond_after_sync, state, sync_frame
from .story.tasks import drain_event_task_sync

_log = get_logger(__name__)

# NameErrorCode（common/systemconst.lua）
NAME_OK = 8001
NAME_INVALID = 8002
NAME_LENGTH = 8004

# 名字长度上限：客户端没给出明确常量【待查真实配置】，这里取保守值。
# 参考：SignatureMaxLen=100 / FormationNameByteLimit=12，玩家名一般 <= 12。
NAME_MAX_LEN = 12


def _clean_name(v):
    """把客户端传来的名字转成 str 并去掉首尾空白。"""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        try:
            v = bytes(v).decode("utf-8")
        except Exception:
            return ""
    return str(v).strip()


def _validate_name(name):
    """返回 (ok, errorcode)。"""
    if not name:
        return False, NAME_INVALID
    if len(name) > NAME_MAX_LEN:
        return False, NAME_LENGTH
    for ch in name:
        o = ord(ch)
        if o < 0x20 or o == 0x7f:
            return False, NAME_INVALID
    return True, NAME_OK


def _set_name(st, name, gender=None, syncs=None):
    """写 player_name（可选 gender）+ 下发属性同步帧。返回是否真的改了。

    ★ 为什么必须下发同步帧（2026-10-23 实机客户端日志实锤）：
      客户端 creatingrolepanel.lua:92-115 submitNameAndGender 成功后**不在本地
      设 player_name / gender**，只播动画 + 上报 SDK，完全依赖服务端同步帧。
      以前这里只写 st.props、不发帧，于是客户端 GlobalData.Avatar.player_name
      一直是 ""、gender 一直是 0，导致：
        stage/streetstage.lua:167 checkIsCanEnterStreet 条件 2/3 失败
        -> 弹 SysTextData[29]「未知的神秘力量干扰了加载进程」-> returnToLogin
      重登后靠 initDict 快照才正常，所以表现为「第一次进街区失败、重登能进」。

    syncs 为 None 时只写状态（给不关心同步的调用方/测试用）。
    """
    changed = False
    if st.props.get("player_name") != name:
        st.props["player_name"] = name
        changed = True
        if syncs is not None:
            try:
                syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                                        "player_name", name))
            except Exception as e:
                _log.warning("[avatar] 玩家名同步失败: {}".format(e))
    if gender is not None:
        try:
            g = int(gender)
        except Exception:
            g = None
        if g in (1, 2) and st.props.get("gender") != g:
            st.props["gender"] = g
            changed = True
            if syncs is not None:
                try:
                    syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                                            "gender", g))
                except Exception as e:
                    _log.warning("[avatar] 性别同步失败: {}".format(e))
    return changed


def _emit_sign(st, syncs):
    """发 Sign 事件（完成取名）。

    ★ 这条事件是 TaskData[10]「完成取名」的唯一完成信号
      （TaskEventData[1000]，EventType=Sign，无 EventParam）。

    ★ P1 阶段 N-a3 已接入：事件总线会把事件同时喂给通用任务（task_bag），
      达到 TriggerTimes 的通用任务会被自动完成，同步帧由调用方 drain 出去。
    """
    from .story import events as _events
    from .story.rpc import _finish_story_tasks
    try:
        res = _events.apply_event(st, _events.ev_sign())
    except Exception as e:
        _log.warning("[avatar] Sign 事件失败: {}".format(e))
        return
    syncs.extend(drain_event_task_sync(st, res))
    hits = list(getattr(res, "hits", []) or [])
    if hits:
        syncs.extend(_finish_story_tasks(st, hits, source="event") or [])


def _name_rpc_common(rid, params, uid, want_gender):
    """三个取名 RPC 的公共骨架。返回 (syncs, st, name, ok, code)。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return syncs, None, "", False, NAME_INVALID
    name = _clean_name(vals[0])
    ok, code = _validate_name(name)
    if not ok:
        # 客户端 checkNameResult 会读 ret.errorcode 弹对应提示
        return (respond_after_sync(rid, [],
                                  ret={"status": False, "errorcode": code}),
                None, name, False, code)
    st = state(uid)
    return syncs, st, name, True, code


def _check_nickname(rid, params, uid=None):
    """RpcCheckNickName {name} —— 取名前的合法性检查（不落库）。"""
    syncs, st, name, ok, code = _name_rpc_common(rid, params, uid, False)
    if ok:
        _log.info("[avatar] 名字检查通过: {}".format(name))
    else:
        _log.info("[avatar] 名字检查不通过: {!r} code={}".format(name, code))
    return respond_after_sync(rid, syncs)


def _change_nickname(rid, params, uid=None):
    """RpcChangeNickName {name} —— 改名（已取过名之后的修改）。

    ★ 改名**不发 Sign 事件**：Sign 语义是「完成取名」，只在首次取名时发。
    """
    syncs, st, name, ok, code = _name_rpc_common(rid, params, uid, False)
    if not ok:
        return syncs
    old = st.props.get("player_name")
    changed = _set_name(st, name, None, syncs)
    if changed:
        st.save()
        _log.info("[avatar] 改名 {!r} -> {!r}".format(old, name))
    else:
        _log.info("[avatar] 改名：名字没变（{!r}）".format(name))
    return respond_after_sync(rid, syncs)



# ★ P1-2（2026-10-23 实机实锤）：按性别设「街区/战斗主角」。
#
# 根因：取名时只写了 player_name / gender / player_card_id，
#   而客户端街区主角读 `character_choosen_char`
#   （system/streetsystem/manager/streetplayerrolemgr.lua:17、
#    streetactorbase.lua:237）。initDict 里它是 9001（男主），
#   取名选女（gender=2）后没切到 9002，也没有 characterBag[9002]，
#   于是「选女主却显示男导演」（存档实锤 nvdaoces）。
#
# 证据：
#   TableData.CommonData.CommonConstData
#     male_player_char   = 9001
#     female_player_char = 9002
#   TableData.CommonData.CharData
#     9001 -> DefaultSkin 900101 / 9002 -> DefaultSkin 900201
#   clientavatar 主角 = character_choosen_char
def _ensure_hero_by_gender(st, gender, syncs):
    """取名时按性别确保主角存在并切换 character_choosen_char。

    gender=1 -> 男主 9001（initDict 已给，无需改）
    gender=2 -> 女主 9002（按客户端 CharacterInfo 声明新建，皮肤取
                CharData[9002].DefaultSkin）
    返回是否改动（好让调用方决定 save）。
    """
    try:
        g = int(gender)
    except Exception:
        return False
    if g not in (1, 2):
        return False
    want = 9002 if g == 2 else 9001
    cb = st.props.get("characterBag")
    if not isinstance(cb, dict):
        return False
    changed = False
    if want not in cb:
        # ★ 2026-10-23：不再「深拷贝另一个角色再改皮肤号」。
        #   深拷贝会把源角色的装备/皮肤/养成进度一起复制（9001 玩过之后再建
        #   9002 就会继承），而且 193 个角色不可能逐个特例。
        #   现在按客户端声明的 CharacterInfo 建干净实例，useSkinId 取
        #   CharData[want].DefaultSkin（9001 -> 900101 / 9002 -> 900201）。
        from .property import factory as _factory
        cb[want] = _factory.make_character(st.props, want)
        changed = True
        print("[avatar] 按 CharacterInfo 声明建角色 {}（useSkinId={}）".format(want, cb[want].get("useSkinId")), flush=True)
    cur = st.props.get("character_choosen_char")
    if int(cur or 0) != want:
        st.props["character_choosen_char"] = want
        changed = True
        try:
            syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                                    "character_choosen_char", want))
        except Exception as e:
            _log.warning("[avatar] 主角同步失败: {}".format(e))
        print("[avatar] 主角切换 gender={} -> character_choosen_char={}".format(g, want), flush=True)
    if changed:
        st.set("characterBag", cb)
    # ★ 2026-10-23 实机反馈：街区右下角队伍切换头像还是男主。
    #   客户端 StreetV2Panel:initFormation（streetv2panel.lua:1311-1341）
    #   用 formation.value[i] 当 charId 给队员设头像，而编队（character_list）
    #   里第一队还是 initDict 的 [9001,0,0]（男主）。这里把「另一性别的导演」
    #   换成主角 want。
    _set_formation_hero(st, want, syncs)

    return changed


def _set_formation_hero(st, want, syncs):
    """把编队（character_list）里「另一性别的导演」换成 want，并下发同步。

    依据 game_server/property/setters.py::_set_character_list 的同步方式：
      propertyId = character_list.__id__，name = 队伍下标，value = 整队对象。

    为什么：客户端 StreetV2Panel:initFormation 用 formation.value[i] 当 charId
    给每个队员设头像（streetv2panel.lua:1311-1341），编队里若还是 9001
    （男主），街区右下角队伍切换就会显示男主头像。
    """
    cl = st.props.get("character_list")
    if not isinstance(cl, dict):
        return
    other = 9001 if want == 9002 else 9002
    changed_teams = []
    for idx, team in list(cl.items()):
        if str(idx).startswith("__") or not isinstance(team, dict):
            continue
        lst = team.get("value")
        if not isinstance(lst, dict):
            continue
        hit = False
        for k, v in list(lst.items()):
            if str(k).startswith("__"):
                continue
            try:
                if int(v) == other:
                    lst[k] = want
                    hit = True
            except Exception:
                continue
        if hit:
            changed_teams.append((idx, team))
    if not changed_teams:
        return
    st.set("character_list", cl)
    st.save()
    for idx, team in changed_teams:
        try:
            if syncs is not None:
                syncs.append(sync_frame(SYNC_SELF, cl.get("__id__") or 0,
                                         msmod.OP_CHANGE, str(idx), team))
        except Exception as e:
            _log.warning("[avatar] 编队同步失败: {}".format(e))
    print('[avatar] 编队主角切换 -> {}（{} 队）'.format(want, len(changed_teams)),
          flush=True)


def _set_default_card(st, gender, syncs):
    """按性别设默认名片（P0-7b）。

    ★ 为什么必须在这里写、不能在 initDict 里定死：
      名片 id 依赖 gender（男=2 女=1），而新号 gender=0（未选）。
      initDict 只给 portrait_id（Default_PlayerHead=1，不依赖性别），
      名片必须等取名拿到 gender 之后再写。

    ★ 证据（为什么是 2/1）：
      TableData.CommonData.CommonConstData
        male_player_card   = 2
        female_player_card = 1
      TableData.CommonData.PlayerCardData[1] / [2] 都是 type=2 name=导演
        （男女主的默认名片）

    ★ 为什么必须给 bag 又给 id：
      PlayerCardData **没有 type=0 默认项**（82 条全是 type 1/2），
      客户端 isCardUnlock(id) = bag:find(id)，空 bag 下完全不能选。
      bag 由 tools/build_avatar_init.py 的 STARTER_FASHIONS 给 {1,2}，
      这里补的是「选中哪一张」。
    """
    try:
        g = int(gender)
    except Exception:
        return
    if g not in (1, 2):
        return
    card_id = 2 if g == 1 else 1
    if st.props.get("player_card_id") == card_id:
        return
    st.props["player_card_id"] = card_id
    try:
        syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE,
                                "player_card_id", card_id))
    except Exception as e:
        _log.warning("[avatar] 名片同步失败: {}".format(e))
    print("[avatar] 按性别设默认名片 gender={} -> player_card_id={}".format(g, card_id), flush=True)


def _commit_name_gender(rid, params, uid=None):
    """RpcCommitNameGender {name, gender} —— 首次取名 + 性别，并触发 Sign 事件。

    这是 TaskData[10]「完成取名」的触发点（见 _emit_sign 的说明）。
    """
    syncs, st, name, ok, code = _name_rpc_common(rid, params, uid, True)
    if not ok:
        return syncs
    vals = decode_params(params)
    gender = vals[1] if len(vals) > 1 else None
    old = st.props.get("player_name")
    changed = _set_name(st, name, gender, syncs)
    if changed:
        st.save()
        _log.info("[avatar] 取名 {!r} -> {!r} gender={}".format(old, name, gender))
    # P0-7b：按性别设默认名片（男2/女1，见 _set_default_card 的说明）
    _set_default_card(st, gender, syncs)
    # P1-2：按性别设街区/战斗主角（女=9002，男=9001）
    _ensure_hero_by_gender(st, gender, syncs)
    # ★ 无论名字是否变化都发 Sign：客户端可能重试同一次取名请求
    _emit_sign(st, syncs)
    st.save()
    return respond_after_sync(rid, syncs)