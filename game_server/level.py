#!/usr/bin/env python3
"""game_server.level —— 玩家等级 / 导演头衔（H3）。

## 这个模块凭什么存在

以前 `level` 和 `exp` 是**任何人都能直接写**的普通根属性：
管理接口 `admin_api._grant_player` 直接 `st.props["level"] = lv`，
没有任何地方管「经验够不够」「升级奖励领过没有」。
于是「等级能升」这件事根本不存在——客户端点了没反应，服务端也没有裁判。

本模块是**等级的唯一写入口**（INV-LV-1）：

    level.upgrade(st)                  升一级（扣 exp / level+1 / 累加 all_exp / 调体力上限）
    level.grant_level_award(st, level) 领某一级的升级奖励（幂等）

全仓不允许第二处写 `props["level"]` / `props["exp"]`；
管理接口的 `/lv` 命令也改走这里。

## 数据依据（全部现场核对过，不要凭记忆改）

`LevelExpData`（BDD，146 行）:

    id            1..146                  等级
    expNeed       每级都有                本级升下一级要多少经验
    awardItem     145 行有（Lv1 没有）     升级奖励 {itemId: count}
    maxStaminaAdd 每级都有                体力上限加成（stamina.max_stamina 已在读）
    titleStage / titleStar                只影响展示，服务端不用算

客户端读取点：

    entities/avatarcomponent/avatarlevelcomp.lua
      :47-49   Cmd_UpgradeLevel_CS(cb)          serverRpc(..., {})      无参数
      :51-55   Cmd_UpgradeLevel_SC(cb, ret)     只看 ret.status
      :57-59   Cmd_FetchLevelupAward_CS(level)  serverRpc(..., {level})
      :61-63   Cmd_FetchTitelVitAward_CS(cb)    serverRpc(..., {})
      :36-45   hasAvailableLevelupAward()       levelup_awards[i] == 0 判未领
      :81-109  onAvatarLevelChanged             刷 UI / 红点 / 活动红点

    ui/panel/directortitlev2panel/directortitlev2panel.lua
      :172-188 refreshExp()   exp >= expNeed 就**自动**调升级 RPC
                             （所以服务端必须能挡住连发，不能靠客户端）
      :136-140 onGoToUpgradeBtnClick()

    ui/panel/playerphasepanel/playerphasepanel.lua
      :191/228/283/366  isObtained = levelup_awards[id] == 1
      :357-361          onButtonReceiveClick -> Cmd_FetchLevelupAward_CS(level)

## 不变量（tests/test_player_level.py 逐条可红）

    INV-LV-1  等级只能通过 upgrade() 改
    INV-LV-2  0 <= exp < expNeed(level)；满级后 exp 不再受限
    INV-LV-3  升级原子：先算完再写，任一步不满足 => props 一个字节都不变
    INV-LV-4  levelup_awards[level] 只能 0/缺失 -> 1，领第二次不再发道具
    INV-LV-5  经验只从「发道具 2」进来（ItemData[2] autoUse），本模块不凭空加 exp
    INV-LV-6  等级变化必须发 AvatarAttr{level} 事件，且不按 level 去重

## 经验从哪来（别再来问）

全 BDD 唯一发玩家经验的是道具 `ItemData[2]`「执业经验」：
`useType=avatarAttr` + `useParam={Exp:1}` + **`autoUse=true`**。
`autoUse` 意味着「获得即使用」，走 `bag.effects.apply_auto_use` ->
`item_use._avatar_attr` -> `PropOp("exp", N)` -> `props["exp"] += N`。

谁发道具 2 由 `TaskData.AwardItemList` 决定，共 184 条：
头衔挑战 175 条 / 头衔日常 8 条 / 新手 1 条（5000004）。

**所以「发经验」不需要新代码**，本模块只管「拿经验换等级」。
"""
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.log_tee import get_logger

from . import gamedata, stamina
from .property import tx as _tx

_log = get_logger(__name__)


class StateLike(Protocol):
    """player_state.PlayerState 的最小接口（避免领域层反向依赖存档实现）。"""

    props: dict[str, Any]

    def set(self, name: str, value: Any) -> Any: ...  # noqa: ANN401

    def next_property_id(self) -> int: ...

    def save(self) -> object: ...

#: 玩家等级根属性
PROP_LEVEL = "level"
#: 当前等级内已积累的经验
PROP_EXP = "exp"
#: 历史累计投入升级的经验（= 已消耗 expNeed 之和；客户端只挂回调没有 UI 读）
PROP_ALL_EXP = "all_exp"
#: 升级奖励领取记录：IntDictKeyInt，level -> 1（config.lua:8707）
PROP_AWARDS = "levelup_awards"


# ---------------------------------------------------------------- 数据读取
def exp_need(level: int) -> int | None:
    """升到 level+1 需要多少经验。等级不存在返回 None。"""
    row = gamedata.level_exp(int(level))
    if not isinstance(row, dict):
        return None
    need = row.get("expNeed")
    if need is None:
        return None
    try:
        return int(need)
    except (TypeError, ValueError):
        return None


def max_level() -> int:
    """等级上限（LevelExpData 里最大的 id）。

    ★ 不写死 146：数据扩表时自动跟随（方案 §七 B6）。
    """
    table = gamedata.table("LevelExpData") or {}
    rows = table.values() if isinstance(table, dict) else table
    best = 0
    for row in rows:
        if isinstance(row, dict):
            try:
                best = max(best, int(row.get("id") or 0))
            except (TypeError, ValueError):
                continue
    return best


def award_of(level: int) -> dict[int, int]:
    """某一级的升级奖励 {itemId: count}；没有奖励返回空 dict。"""
    row = gamedata.level_exp(int(level))
    if not isinstance(row, dict):
        return {}
    raw = row.get("awardItem")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for key, val in raw.items():
        if isinstance(key, str) and (key.startswith("__") or key == "KeyList"):
            continue
        try:
            item_id, count = int(key), int(val)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[item_id] = count
    return out


# ---------------------------------------------------------------- 状态读取
def _int_prop(props: dict[str, Any], name: str, default: int = 0) -> int:
    val = props.get(name)
    if isinstance(val, bool) or not isinstance(val, int):
        try:
            return int(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return val


def current_level(props: dict[str, Any]) -> int:
    return _int_prop(props, PROP_LEVEL, 1)


def current_exp(props: dict[str, Any]) -> int:
    return _int_prop(props, PROP_EXP, 0)


def can_upgrade(props: dict[str, Any]) -> bool:
    """现在能不能升一级（经验够 + 没满级）。"""
    level = current_level(props)
    need = exp_need(level)
    if need is None:                      # 满级：LevelExpData 里没有 level+1
        return False
    return current_exp(props) >= need


def award_claimed(props: dict[str, Any], level: int) -> bool:
    """这一级的升级奖励领过没有（客户端判 == 1）。"""
    raw = props.get(PROP_AWARDS)
    if not isinstance(raw, dict):
        return False
    return raw.get(int(level)) == 1


def ensure_award_slots(props: dict[str, Any]) -> int:
    """给每一级有奖励的等级补一个**显式的 0**。返回补了几个。

    ★ 为什么必须补（同源 Lua 实测，2026-10-28）：

        avatarlevelcomp.lua:36-45
            for i = 1, level do
              if tableData[i].awardItem and self.levelup_awards[i] == 0 then
                return true
              end
            end
            return false

      客户端判的是 `== 0`，不是「不等于 1」。
      实测（tests/lua_harness 跑真组件）：

           awards[2] = nil -> hasAvailableLevelupAward() = false   ← 红点不亮、玩家不知道能领
           awards[2] = 0   -> hasAvailableLevelupAward() = true    ← 正确
           awards[2] = 1   -> false                                ← 已领，正确

      所以「领了才写 1」是不够的 —— **未领的必须显式写 0**。
      不补这一手，玩家升了级却看不到「有奖励可领」的红点，
      而 playerphasepanel 那条路径用的是 `== 1`（nil 也判未领，能正常显示），
      两处判定不一致 → 只有红点坏掉，最难发现的那种。

    归一化：缺失 -> 0，已领的 1 保持不变。幂等。
    """
    raw = props.get(PROP_AWARDS)
    if not isinstance(raw, dict):
        return 0
    added = 0
    for lv_str in award_levels():
        if lv_str not in raw:
            raw[lv_str] = 0
            added += 1
    return added


def award_levels() -> list[int]:
    """所有「有升级奖励」的等级（LevelExpData.awardItem 非空）。"""
    table = gamedata.table("LevelExpData") or {}
    rows = table.values() if isinstance(table, dict) else table
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("awardItem"), dict):
            continue
        raw_id = row.get("id")
        if raw_id is None:
            continue
        try:
            lv = int(raw_id)
        except (TypeError, ValueError):
            continue
        if lv >= 1:
            out.append(lv)
    return sorted(out)


def pending_awards(props: dict[str, Any]) -> list[int]:
    """已达成但还没领奖励的等级列表（客户端红点用 hasAvailableLevelupAward）。"""
    level = current_level(props)
    out = []
    for lv in range(1, level + 1):
        if award_of(lv) and not award_claimed(props, lv):
            out.append(lv)
    return out


# ---------------------------------------------------------------- 结果类型
@dataclass
class UpgradeResult:
    ok: bool
    reason: str = ""
    old_level: int = 0
    new_level: int = 0
    old_exp: int = 0
    new_exp: int = 0
    spent: int = 0
    frames: list[bytes] = field(default_factory=list)


@dataclass
class AwardResult:
    ok: bool
    reason: str = ""
    level: int = 0
    already: bool = False        # 之前已经领过（幂等命中）
    granted: list[tuple[int, int]] = field(default_factory=list)
    frames: list[bytes] = field(default_factory=list)


# ---------------------------------------------------------------- 唯一写入口
def upgrade(st: StateLike) -> UpgradeResult:
    """升一级。**这是 `level` 的唯一写入口**（INV-LV-1）。

    顺序（INV-LV-3 原子性）：所有判断先做完，任何一条不满足就**不写任何东西**，
    然后才一次性写 props。这样不会出现「扣了经验没升级」。

    返回 UpgradeResult；ok=False 时 props 未被修改。
    """
    props = st.props
    old_level = current_level(props)
    old_exp = current_exp(props)
    need = exp_need(old_level)
    cap = max_level()

    # ---- ① 判断（一条都不写）----
    if need is None or old_level >= cap:
        return UpgradeResult(False, f"已满级（{old_level}/{cap}）", old_level, old_level, old_exp, old_exp)
    if old_exp < need:
        return UpgradeResult(False, f"经验不足（{old_exp}/{need}）", old_level, old_level, old_exp, old_exp)

    new_level = old_level + 1
    new_exp = old_exp - need

    # ---- ② 写入（全部在同一个 Tx，任一帧生成失败都还没 save）----
    # INV-LV-4 补充：未领的奖励等级必须显式写 0（客户端红点判 == 0，不是 != 1）。
    # 同源 Lua 实测见 ensure_award_slots 的注释。
    tx = _tx.Tx(props)
    tx.set((PROP_EXP,), new_exp)
    tx.set((PROP_LEVEL,), new_level)
    tx.set((PROP_ALL_EXP,), _int_prop(props, PROP_ALL_EXP, 0) + need)

    # 体力上限随等级变（LevelExpData.maxStaminaAdd）。
    # ★ R2：只调上限，**不补体力** —— 补了就是刷体力漏洞。
    #   上限变大时当前值不变；上限没变时什么都不做。
    _clamp_stamina(props, new_level, tx)
    _fill_award_slots(props, tx, new_level)

    frames = tx.frames()
    # 根属性同步：exp / level / all_exp 都已由 tx.set 产出 CHANGE（propertyId=0）
    _log.info("[level] 升级 %s -> %s（消耗 %s exp，剩余 %s）", old_level, new_level, need, new_exp)

    # ---- ③ 体力上限变了要额外推两个时间戳（stamina 模块的契约）----
    frames.extend(_stamina_frames(st, new_level))

    return UpgradeResult(True, "", old_level, new_level, old_exp, new_exp, need, frames)


def _fill_award_slots(props: dict[str, Any], tx: _tx.Tx, level: int) -> None:
    """给「已达成且有奖励」的等级补显式 0（红点判 == 0，见 ensure_award_slots）。

    只补 level 以内、还没领过的；已领的 1 不动。
    通过 Tx 写，保证客户端收到 OP_ADD（新键）或 OP_CHANGE。
    """
    raw = props.get(PROP_AWARDS)
    if not isinstance(raw, dict):
        return
    for lv in range(1, level + 1):
        if lv in raw:
            continue
        if not award_of(lv):
            continue
        tx.set((PROP_AWARDS, lv), 0)


def _clamp_stamina(props: dict[str, Any], level: int, tx: _tx.Tx) -> None:
    """把当前体力压到新上限以内（只降不升）。"""
    cur = _int_prop(props, stamina.PROP_KEYS, 0)
    cap = stamina.max_stamina(level)
    if cur > cap:
        tx.set((stamina.PROP_KEYS,), cap)
        _log.info("[level] 体力 %s 超过新上限 %s，压到上限", cur, cap)


def _stamina_frames(st: StateLike, level: int) -> list[bytes]:
    """体力上限变了以后，把「恢复满的时间戳」重算一遍推给客户端。

    客户端 node_rogue_infogroup.lua:114-132 用两个时间戳显示「xx:xx:xx 后恢复满」；
    上限变了但时间戳不更新 => 显示的上限是旧的。
    """
    from .rpc_common import SYNC_SELF, sync_frame

    props = st.props
    keys = _int_prop(props, stamina.PROP_KEYS, 0)
    cap = stamina.max_stamina(level)
    frames: list[bytes] = []
    nxt = _int_prop(props, stamina.PROP_NEXT, 0)
    if keys >= cap:
        # 满了：两个时间戳都归 0（客户端据此显示「已满」）
        for name in (stamina.PROP_NEXT, stamina.PROP_TOTAL):
            if _int_prop(props, name, 0) != 0:
                props[name] = 0
                frames.append(sync_frame(SYNC_SELF, 0, _tx.OP_CHANGE, name, 0))
    elif nxt > 0:
        total = nxt + (cap - keys - 1) * stamina.refresh_sec()
        if _int_prop(props, stamina.PROP_TOTAL, 0) != total:
            props[stamina.PROP_TOTAL] = total
            frames.append(sync_frame(SYNC_SELF, 0, _tx.OP_CHANGE, stamina.PROP_TOTAL, total))
    return frames


def grant_level_award(st: StateLike, level: int) -> AwardResult:
    """领取某一级的升级奖励（INV-LV-4 幂等）。

    能领的条件：1 <= level <= 当前等级，且 LevelExpData[level].awardItem 非空。
    已领过的重复请求**回成功但不再发道具**（客户端可能重复点）。
    """
    from .property.bag import grants as _grants
    from .rpc_common import SYNC_SELF, sync_frame

    props = st.props
    try:
        level = int(level)
    except (TypeError, ValueError):
        return AwardResult(False, "等级参数非法")
    cur = current_level(props)
    if level < 1:
        return AwardResult(False, f"等级 {level} 非法", level)
    if level > cur:
        return AwardResult(False, f"等级 {level} 还没达到（当前 {cur}）", level)
    items = award_of(level)
    if not items:
        return AwardResult(False, f"等级 {level} 没有升级奖励", level)
    if award_claimed(props, level):
        _log.info("[level] 等级 %s 的奖励已经领过，忽略重复请求", level)
        return AwardResult(True, "", level, already=True)

    # 发奖走唯一发放入口（堆叠 / maxNum / notStack / ext / autoUse 全在里面）
    _tx_bag, outcomes = _grants.grant_many(props, items, source=f"level_award:{level}")
    frames = _grants.sync_frames(props, outcomes)
    granted: list[tuple[int, int]] = []
    for oc in outcomes:
        if oc.ok:
            granted.append((oc.item_id, oc.count))
        else:
            _log.warning("[level] 等级 %s 奖励 %s 发放失败：%s", level, oc.item_id, oc.error)

    awards = props.get(PROP_AWARDS)
    if not isinstance(awards, dict):
        awards = {"__id__": st.next_property_id()}
        props[PROP_AWARDS] = awards
    awards_id = awards.get("__id__") or 0
    awards[level] = 1
    st.set(PROP_AWARDS, awards)
    frames.append(sync_frame(SYNC_SELF, awards_id, _tx.OP_CHANGE, str(level), 1))
    _log.info("[level] 领取 %s 级奖励 -> %s", level, granted)
    return AwardResult(True, "", level, False, granted, frames)


# ---------------------------------------------------------------- 兼容旧写入口
def set_level(st: StateLike, level: int) -> None:
    """把等级**直接设成**某个值（GM / 管理接口用）。

    ★ 这不是「升级」，是调试用的强制覆盖。刻意留在本模块里，
      让「谁改过 level」只有一处可以 grep 到（INV-LV-1）。
      正常玩法路径永远走 upgrade()。
    """
    level = int(level)
    if level < 1:
        raise ValueError(f"等级必须 >= 1，收到 {level}")
    cap = max_level()
    if cap and level > cap:
        raise ValueError(f"等级 {level} 超过数据上限 {cap}")
    st.set(PROP_LEVEL, level)
