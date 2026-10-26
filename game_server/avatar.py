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


def validate_name(name):
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
                _log.warning(f"[avatar] 玩家名同步失败: {e}")
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
                    _log.warning(f"[avatar] 性别同步失败: {e}")
    return changed


def _emit_sign(st, syncs):
    """发 Sign 事件（完成取名）。

    ★ 这条事件是 TaskData[10]「完成取名」的唯一完成信号
      （TaskEventData[1000]，EventType=Sign，无 EventParam）。

    ★ P1 阶段 N-a3 已接入：事件总线会把事件同时喂给通用任务（task_bag），
      达到 TriggerTimes 的通用任务会被自动完成，同步帧由调用方 drain 出去。
    """
    from .story import events as _events
    from .story.rpc import finish_story_tasks
    try:
        res = _events.apply_event(st, _events.ev_sign())
    except Exception as e:
        _log.warning(f"[avatar] Sign 事件失败: {e}")
        return
    syncs.extend(drain_event_task_sync(st, res))
    hits = list(getattr(res, "hits", []) or [])
    if hits:
        syncs.extend(finish_story_tasks(st, hits, source="event") or [])


def _name_rpc_common(rid, params, uid, _want_gender):
    """三个取名 RPC 的公共骨架。返回 (syncs, st, name, ok, code)。"""
    syncs = []
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return syncs, None, "", False, NAME_INVALID
    name = _clean_name(vals[0])
    ok, code = validate_name(name)
    if not ok:
        # 客户端 checkNameResult 会读 ret.errorcode 弹对应提示
        return (respond_after_sync(rid, [],
                                  ret={"status": False, "errorcode": code}),
                None, name, False, code)
    st = state(uid)
    return syncs, st, name, True, code


def _check_nickname(rid, params, uid=None):
    """RpcCheckNickName {name} —— 取名前的合法性检查（不落库）。"""
    syncs, _st, name, ok, code = _name_rpc_common(rid, params, uid, False)
    if ok:
        _log.info(f"[avatar] 名字检查通过: {name}")
    else:
        _log.info(f"[avatar] 名字检查不通过: {name!r} code={code}")
    return respond_after_sync(rid, syncs)


def _change_nickname(rid, params, uid=None):
    """RpcChangeNickName {name} —— 改名（已取过名之后的修改）。

    ★ 改名**不发 Sign 事件**：Sign 语义是「完成取名」，只在首次取名时发。
    """
    syncs, st, name, ok, _code = _name_rpc_common(rid, params, uid, False)
    if not ok:
        return syncs
    old = st.props.get("player_name")
    changed = _set_name(st, name, None, syncs)
    if changed:
        st.save()
        _log.info(f"[avatar] 改名 {old!r} -> {name!r}")
    else:
        _log.info(f"[avatar] 改名：名字没变（{name!r}）")
    return respond_after_sync(rid, syncs)



# 主角（导演）管理：唯一入口是 game_server/protagonist.py。
#
# 语义（2026-10-27 重构）：
#   · 新号没有角色（avatar_init 不再预置 9001）；
#   · 取名提交时按 gender 创建**唯一**主角，并同步 characterBag / 编队 / choosen；
#   · 登录时做互斥自愈：只保留与 gender 一致的那个（双主角存档会被清理）。
#
# 证据：
#   CommonConstData.male_player_char / female_player_char（BDD 9001 / 9002）
#   CharData[9001].DefaultSkin=900101 / CharData[9002].DefaultSkin=900201
#   streetstage.lua:167-182：进街区要求「已取名 + gender!=0 + 至少一个主角存在」
def _ensure_hero_by_gender(st, gender, syncs=None):
    """兼容旧名：按性别确保唯一主角（真正实现在 protagonist.ensure）。"""
    from . import protagonist

    return protagonist.ensure(st, gender, syncs)


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
        _log.warning(f"[avatar] 名片同步失败: {e}")
    _log.info(f"[avatar] 按性别设默认名片 gender={g} -> player_card_id={card_id}")


def _commit_name_gender(rid, params, uid=None):
    """RpcCommitNameGender {name, gender} —— 首次取名 + 性别，并触发 Sign 事件。

    这是 TaskData[10]「完成取名」的触发点（见 _emit_sign 的说明）。
    """
    syncs, st, name, ok, _code = _name_rpc_common(rid, params, uid, True)
    if not ok:
        return syncs
    vals = decode_params(params)
    gender = vals[1] if len(vals) > 1 else None
    old = st.props.get("player_name")
    changed = _set_name(st, name, gender, syncs)
    if changed:
        st.save()
        _log.info(f"[avatar] 取名 {old!r} -> {name!r} gender={gender}")
    # P0-7b：按性别设默认名片（男2/女1，见 _set_default_card 的说明）
    _set_default_card(st, gender, syncs)
    # 主角：按性别创建唯一主角（新号没有角色，见 protagonist.py）
    _ensure_hero_by_gender(st, gender, syncs)
    # ★ 无论名字是否变化都发 Sign：客户端可能重试同一次取名请求
    _emit_sign(st, syncs)
    st.save()
    return respond_after_sync(rid, syncs)
