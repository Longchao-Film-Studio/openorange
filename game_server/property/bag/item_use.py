"""使用道具（**唯一入口**）。

流程（顺序固定，缺一不可）：

    ① 定位格子（ext["id"] 优先，itemId 兜底——唯一实例必须按格子定位）
    ② 校验数量 / ext
    ③ 查效果 handler：没有 -> Bag_SystemNotUnlocked，**此刻还没扣任何东西**
    ④ handler 算 ops（不落 props）
    ⑤ 提交：扣数量 + materialize(ops)，同一个 Tx
    ⑥ save + frames

★ 这解决了 2026-10-26 之前的老毛病：老的 _use_item 是「先扣数量、再试效果」，
  效果不支持时返回空列表 —— 道具被吃掉、什么都没有。
"""
import time
from typing import Any, Protocol

from shared.log_tee import get_logger

from ... import gamedata
from ..tx import Tx
from . import effects, grants, report
from .effects import BagAddOp, CustomOp, GrantOp, Op, PropOp
from .errors import (
    BAG_INVALID_EXT_PARAMS,
    BAG_INVALID_ID,
    BAG_INVALID_ITEM_ID,
    BAG_INVALID_ITEM_LIST,
    BAG_INVALID_NUM,
    BAG_SYSTEM_NOT_UNLOCKED,
    BagError,
)

_log = get_logger(__name__)

#: avatarAttr 的装饰键 -> 客户端 FashionBag 属性名
#: 依据 avataruserdatacomp.lua:493-532 的 isXxxUnlock 一一对应
DECO_BAGS: dict[str, str] = {
    "Portrait": "portrait_bag",
    "PortraitFrame": "portrait_frame_bag",
    "Card": "player_card_bag",
    "Background": "background_bag",
    "Title": "player_title_bag",
    "Bubble": "chat_bubble_bag",
    "StickerId": "emoji_bag",
}

#: avatarAttr 的计数器键 -> 属性名（其余键是通行证/好感等，P4 处理）
COUNTER_PROPS: dict[str, str] = {
    "Exp": "exp",
    "Crystal": "crystal",
    "TowerRewardKeys": "tower_reward_keys",
}


class StateLike(Protocol):
    """player_state.PlayerState 的最小接口（避免 bag 包反向依赖）。"""

    props: dict[str, Any]

    def save(self) -> object: ...


def _params(cfg: dict[str, Any]) -> dict[str, Any]:
    p = cfg.get("useParam")
    return p if isinstance(p, dict) else {}


def _key_list(cfg: dict[str, Any]) -> list[str]:
    kl = _params(cfg).get("KeyList")
    return [str(k) for k in kl] if isinstance(kl, list) else []


#: 通行证经验键（avatarAttr 的一种）：镜像 bpinfo.lua:50-57 的 add_bp_exp
#:   BpExp        is_season_exp=0 -> bp_exp / all_bp_exp / week_bp_exp，并刷 bp_exp_time
#:   BpExpSeason  is_season_exp=1 -> 只加 bp_exp / all_bp_exp
#: ★ 等级（bp_level）由官方服务端算，BDD 里没有等级阈值表（BattlePassData 只有 10 个赛季行），
#:   所以这里只累加经验，等级提升留待拿到阈值表（见 ROADMAP 补7 待定项）。
_BP_EXP_KEYS: dict[str, bool] = {"BpExp": True, "BpExpSeason": False}


def _apply_bp_exp(props: dict[str, Any], key: str, add: int) -> list[bytes]:
    from ... import ms as msmod
    from ...rpc_common import SYNC_SELF, sync_frame

    info = props.get("bp_info")
    if not isinstance(info, dict):
        report.note("bp", key, "bp_info 不存在")
        return []
    pid = info.get("__id__") or 0
    frames: list[bytes] = []

    def bump(field: str, delta: int) -> None:
        cur = info.get(field)
        base = int(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else 0
        info[field] = base + int(delta)
        frames.append(sync_frame(SYNC_SELF, pid, msmod.OP_CHANGE, field, info[field]))

    bump("bp_exp", add)
    bump("all_bp_exp", add)
    if _BP_EXP_KEYS[key]:
        bump("week_bp_exp", add)
    bump("bp_exp_time", int(time.time()))
    return frames


def _bp_exp_op(key: str, value: int) -> CustomOp:
    """把 (键, 数值) 固定成 CustomOp（用命名函数而不是带默认参数的 lambda）。"""

    def _fn(props: dict[str, Any]) -> list[bytes]:
        return _apply_bp_exp(props, key, value)

    return CustomOp(_fn)


@effects.register("avatarAttr")
def _avatar_attr(_props: dict[str, Any], cfg: dict[str, Any], count: int, _ext: dict[str, Any]) -> list[Op]:
    """装饰解锁 + 简单计数器。未登记的键一律报错（不允许静默跳过）。"""
    params = _params(cfg)
    ops: list[Op] = []
    for k in _key_list(cfg):
        try:
            v = int(params.get(k) or 0) * int(count)
        except (TypeError, ValueError):
            raise BagError(BAG_INVALID_EXT_PARAMS, f"useParam[{k}] 不是数字") from None
        if k in _BP_EXP_KEYS:
            # 通行证经验：写 bp_info 子字段（不是根属性），用 CustomOp
            if v:
                ops.append(_bp_exp_op(k, v))
        elif k in DECO_BAGS:
            if v <= 0:
                raise BagError(BAG_INVALID_EXT_PARAMS, f"{k} 的装饰 id 非法")
            ops.append(BagAddOp(DECO_BAGS[k], v, {"id": v, "expiration_time": 0}))
        elif k in COUNTER_PROPS:
            if v:
                ops.append(PropOp(COUNTER_PROPS[k], v))
        else:
            raise BagError(BAG_SYSTEM_NOT_UNLOCKED, f"avatarAttr 键 {k} 暂未支持")
    return ops


@effects.register("item")
def _item_effect(_props: dict[str, Any], cfg: dict[str, Any], count: int, _ext: dict[str, Any]) -> list[Op]:
    """使用后直接发放列出的道具（礼包/咒典全本这类）。"""
    params = _params(cfg)
    ops: list[Op] = []
    for k in _key_list(cfg):
        try:
            n = int(params.get(k) or 0) * int(count)
        except (TypeError, ValueError):
            raise BagError(BAG_INVALID_EXT_PARAMS, f"useParam[{k}] 不是数字") from None
        if n > 0:
            ops.append(GrantOp(int(k), n))
    if not ops:
        raise BagError(BAG_SYSTEM_NOT_UNLOCKED, "item 效果没有可发放内容")
    return ops


@effects.register("selectItem")
def _select_item(_props: dict[str, Any], cfg: dict[str, Any], count: int, ext: dict[str, Any]) -> list[Op]:
    """自选道具：客户端在 ext.itemId 里带选择（genchooseitempanel.lua:171-176）。"""
    chosen = ext.get("itemId")
    if chosen is None:
        raise BagError(BAG_INVALID_EXT_PARAMS, "selectItem 缺少 ext.itemId")
    try:
        chosen = int(chosen)
    except (TypeError, ValueError):
        raise BagError(BAG_INVALID_EXT_PARAMS, "ext.itemId 不是数字") from None
    if chosen not in {int(k) for k in _key_list(cfg)}:
        raise BagError(BAG_INVALID_EXT_PARAMS, "ext.itemId 不在可选列表里")
    return [GrantOp(chosen, int(count))]


@effects.register("randomItem")
def _random_item(props: dict[str, Any], cfg: dict[str, Any], count: int, _ext: dict[str, Any]) -> list[Op]:
    """随机池：useParam.KeyList 是 **PoolId**（不是道具 id）。

    抽 `count` 次；结果里能入包的道具转成 GrantOp。
    若整池都抽不到可入包的道具（池里全是装备/时装等未实现系统的东西），
    直接报 15007 且**不消耗盲盒** —— 宁可让玩家打不开，也不能吞掉。
    """
    from . import pools

    kl = _key_list(cfg)
    if len(kl) != 1:
        raise BagError(BAG_INVALID_EXT_PARAMS, f"randomItem 的 KeyList 应为 1 个池 id，实际 {kl}")
    try:
        pool_id = int(kl[0])
    except (TypeError, ValueError):
        raise BagError(BAG_INVALID_EXT_PARAMS, "池 id 不是数字") from None
    drawn = pools.draw(props, pool_id, count)
    ops: list[Op] = [GrantOp(iid, n) for iid, n in drawn.items if n > 0]
    if not ops:
        raise BagError(BAG_INVALID_ITEM_LIST,
                       f"池 {pool_id} 的奖励都不是道具（等装备/时装系统），本次不消耗")
    return ops

@effects.register("streetPrestige")
def _street_prestige(_props: dict[str, Any], cfg: dict[str, Any], count: int, _ext: dict[str, Any]) -> list[Op]:
    """街头声望：useParam {streetId, prestigeExp}（时砂之外的第二类"街区内生效"道具）。"""
    from . import special

    params = _params(cfg)
    try:
        street_id = int(params.get("streetId") or 0)
        exp = int(params.get("prestigeExp") or 0) * int(count)
    except (TypeError, ValueError):
        raise BagError(BAG_INVALID_EXT_PARAMS, "streetPrestige 参数非法") from None
    if street_id <= 0 or exp == 0:
        raise BagError(BAG_INVALID_EXT_PARAMS, "streetPrestige 缺少 streetId/prestigeExp")
    return [CustomOp(lambda p: special.add_prestige_exp(p, street_id, exp))]


@effects.register("charFavorSpace")
def _char_favor_space(_props: dict[str, Any], cfg: dict[str, Any], count: int, ext: dict[str, Any]) -> list[Op]:
    """送礼（好感度）：必须带 ext.charId —— 客户端送礼面板走 AddCharFavorExp_CS，
    但背包直接使用也要能生效，所以这里同样要求 charId。
    """
    from . import special

    char_id = ext.get("charId")
    if char_id is None:
        raise BagError(BAG_INVALID_EXT_PARAMS, "charFavorSpace 缺少 ext.charId（送礼对象）")
    try:
        char_id = int(char_id)
    except (TypeError, ValueError):
        raise BagError(BAG_INVALID_EXT_PARAMS, "ext.charId 不是数字") from None
    params = _params(cfg)
    try:
        exp = int(params.get("CharFavorExp") or 0) * int(count)
        pop = int(params.get("CharPopularity") or 0) * int(count)
    except (TypeError, ValueError):
        raise BagError(BAG_INVALID_EXT_PARAMS, "charFavorSpace 参数非法") from None
    return [CustomOp(lambda p: special.add_char_favor(p, char_id, exp, pop))]


def materialize(
    props: dict[str, Any], ops: list[Op], *, tx: Tx, source: str = "", depth: int = 0,
) -> tuple[list[tuple[int, int, list[str]]], list[bytes]]:
    """把 handler 算出的 ops 落到 props（同一个 Tx，保证原子）。

    返回**发放明细** [(item_id, count, grid_keys)] —— 供 Avatar_Reward_SC 展示用。
    """
    granted: list[tuple[int, int, list[str]]] = []
    extra: list[bytes] = []
    for op in ops:
        if isinstance(op, PropOp):
            cur = props.get(op.name)
            if not isinstance(cur, (int, float)) or isinstance(cur, bool):
                # 属性不存在：跳过而不是凭空建一个（老实现也是这样；真实存档都有这些属性）
                _log.warning("[bag] 属性 %s 不存在，效果 %s 未生效", op.name, op.value)
                continue
            tx.set((op.name,), cur + op.value)
        elif isinstance(op, GrantOp):
            oc = grants.grant(props, op.item_id, op.count, tx=tx, source=source, depth=depth + 1)
            if oc.ok:
                granted.append((op.item_id, oc.stored or op.count, oc.grids))
        elif isinstance(op, BagAddOp):
            tx.set((op.bag, op.key), dict(op.value))
        elif isinstance(op, CustomOp):
            extra.extend(op.fn(props))
    return granted, extra


def _grid_of(props: dict[str, Any], item_id: int, ext: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """定位格子：ext.id 优先（唯一实例必须按格子定位），否则按 itemId 找第一个。"""
    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        raise BagError(BAG_INVALID_ITEM_ID, "item_bag 不存在")
    grid_id = ext.get("id")
    if grid_id is not None:
        key = str(grid_id)
        grid = bag.get(key)
        if not isinstance(grid, dict):
            raise BagError(BAG_INVALID_ID, f"格子 {key} 不存在")
        if int(grid.get("itemId") or 0) != int(item_id):
            raise BagError(BAG_INVALID_ID, f"格子 {key} 的道具 id 是 {grid.get('itemId')}")
        return key, grid
    for key, grid in bag.items():
        if isinstance(grid, dict) and int(grid.get("itemId") or 0) == int(item_id):
            return str(key), grid
    raise BagError(BAG_INVALID_ITEM_ID, "背包里没有该道具")


def apply(
    st: StateLike, item_id: int, count: int, ext: dict[str, Any] | None = None,
) -> tuple[list[bytes], BagError | None, list[bytes]]:
    """使用道具。

    返回 (同步帧, 错误, 推送帧)。
      · 成功：frames=属性同步，err=None，tail=Avatar_Reward_SC 展示帧
      · 失败：frames=[]，err=BagError，tail=[]，且**不改任何状态**
    """
    props: dict[str, Any] = st.props
    ext = ext if isinstance(ext, dict) else {}
    try:
        item_id = int(item_id)
        count = int(count)
    except (TypeError, ValueError):
        return [], BagError(BAG_INVALID_NUM, "参数非法"), []
    if count <= 0:
        return [], BagError(BAG_INVALID_NUM, "数量必须为正"), []

    cfg = gamedata.item(item_id)
    if not isinstance(cfg, dict) or not cfg:
        return [], BagError(BAG_INVALID_ITEM_ID, "ItemData 里没有这个道具"), []

    try:
        key, grid = _grid_of(props, item_id, ext)
        num = int(grid.get("num") or 0)
        if num < count:
            raise BagError(BAG_INVALID_NUM, f"格子数量 {num} 不足 {count}")

        use_type = gamedata.item_use_type(item_id)
        handler = effects.handler_for(use_type)
        if handler is None:
            reason = (effects.UNSUPPORTED_USE_TYPES.get(use_type)
                      or effects.PENDING_USE_TYPES.get(use_type)
                      or "未登记的 useType")
            report.note("effect", use_type, reason)
            raise BagError(BAG_SYSTEM_NOT_UNLOCKED, f"{use_type}：{reason}")

        ops = handler(props, cfg, count, ext)          # ④ 只算不写

        tx = Tx(props)
        if num == count:
            tx.delete(("item_bag", key))
        else:
            # ★ 整格 set：与客户端 _changeSubProperty 的契约一致（见 grants 注释）
            grid["num"] = num - count
            tx.set(("item_bag", key), grid)
        granted, extra = materialize(props, ops, tx=tx, source=f"use:{item_id}")
        st.save()
        _log.info("[bag] 使用 %s x%s（%s）-> %s 个 ops", item_id, count, use_type, len(ops))
        from . import feedback
        return tx.frames() + extra, None, feedback.reward_frames(granted)
    except BagError as e:
        _log.info("[bag] 使用 %s 失败：%s", item_id, e)
        return [], e, []
