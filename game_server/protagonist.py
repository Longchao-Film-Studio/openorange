"""导演主角（男 / 女）的唯一管理入口。

语义（唯一来源，别再在别处写死 9001/9002）：

  · **新号没有任何角色**（avatar_init 不再预置主角）；
  · `RpcCommitNameGender(name, gender)` 时按性别创建**唯一**主角；
  · 登录自愈：只保留与 `gender` 一致的那个（旧存档里的「双主角」会被清掉）；
  · 编队第 1 队 = [主角, 0, 0]（3 个格子，空位填 0 —— 客户端 initFormation 要求）；
  · `character_choosen_char` 始终指向当前主角。

id 不写死：读 `CommonConstData.male_player_char / female_player_char`
（BDD 实测 9001 / 9002；客户端 `tabledata/commondata/commonconstdata.lua:202-203`）。
"""
from typing import Any

from shared.log_tee import get_logger

from . import gamedata
from .property import factory
from .property.tx import Tx
from .write_guard import mutable

_log = get_logger(__name__)


def _const(name: str) -> int:
    """从数据表取主角 id；缺表直接报错，不给“默认值”。"""
    v = gamedata.const(name)
    if v is None:
        raise RuntimeError(f"CommonConstData 缺少 {name}（主角 id 不能写死）")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise RuntimeError(f"CommonConstData.{name} 不是数字：{v!r}") from None


def male_id() -> int:
    return _const("male_player_char")


def female_id() -> int:
    return _const("female_player_char")


def protagonist_ids() -> tuple[int, int]:
    return male_id(), female_id()


def char_id_for_gender(gender: Any) -> int | None:  # noqa: ANN401 — 参数来自 RPC/存档，类型不可信
    """1 -> 男主角，2 -> 女主角，其余（0/None/非法）-> None。"""
    try:
        g = int(gender)
    except (TypeError, ValueError):
        return None
    if g == 1:
        return male_id()
    if g == 2:
        return female_id()
    return None


def is_protagonist(char_id: Any) -> bool:  # noqa: ANN401 — 同上
    try:
        cid = int(char_id)
    except (TypeError, ValueError):
        return False
    return cid in protagonist_ids()


def _character_bag(props: dict[Any, Any]) -> dict[Any, Any]:
    bag = props.get("characterBag")
    if isinstance(bag, dict):
        return bag
    bag = factory.new_instance(props, "CharacterPersistentDict")
    props["characterBag"] = bag
    return bag


def _formation(props: dict[Any, Any]) -> dict[Any, Any] | None:
    cl = props.get("character_list")
    if not isinstance(cl, dict):
        return None
    return cl


def _set_team_hero(tx: Any, props: dict[Any, Any], want: int) -> None:  # noqa: ANN401
    """把编队第 1 队设成 [want, 0, 0]；其余位置清 0（主角只该有一个）。

    整队换值走 tx.set：客户端对命名容器是「一个键 = 一整队」的语义
    （_changeSubProperty 按 __id__ 命中后原地 rochange），所以不需要拆到槽位。
    """
    cl = _formation(props)
    if cl is None:
        _log.warning("[protagonist] character_list 不存在，跳过编队")
        return
    team = cl.get(1) if isinstance(cl.get(1), dict) else cl.get("1")
    if not isinstance(team, dict):
        _log.warning("[protagonist] 编队 #1 不存在，跳过")
        return
    # ★ 副本 -> Tx：team / slots 是 props 树里的容器，直接改是裸写
    #   （守卫会抛，gate 关连接，客户端 lose server）
    new_team = mutable(team)
    slots = new_team.get("value")
    if not isinstance(slots, dict):
        slots = factory.new_instance(props, "IntList")
    else:
        slots = mutable(slots)
    changed = False
    for i, v in ((1, want), (2, 0), (3, 0)):
        if int(slots.get(i) or 0) != v:
            slots[i] = v
            changed = True
    if changed:
        new_team["value"] = slots
        tx.set(("character_list", 1), new_team)


def ensure_props(props: dict[Any, Any], gender: Any, syncs: list[bytes] | None = None) -> list[bytes]:  # noqa: ANN401
    """核心实现：保证 props 里「只有与 gender 一致的那一个主角」。

    不依赖 PlayerState（脚手架/离线工具只有 props 时也能用）；
    `ensure(st, ...)` 是它的 PlayerState 包装（额外 st.set 标记脏值）。
    """
    frames: list[bytes] = []
    want = char_id_for_gender(gender)
    bag = _character_bag(props)

    # 全部写入走同一个 Tx：帧由 tx.frames() 编，不再逐条手拼。
    tx = Tx(props)

    # 1) 删掉不该存在的（另一性别 / 未选性别时的全部）
    for cid in protagonist_ids():
        if want is not None and cid == want:
            continue
        if cid in bag:
            # 先删子项再写回容器：Tx.delete 会记成 OP_DEL（客户端 __OpDel__）
            tx.delete(("characterBag", cid))
            _log.info("[protagonist] 删除多余主角 %s（want=%s）", cid, want)

    # 2) 补上正确的那个
    # ★ 不能先 bag[want] = ... 再 tx.set：Tx 靠「键在不在父容器里」判断
    #   OP_ADD / OP_CHANGE（tx.py 的 is_new 必须在赋值之前判断）。
    #   先赋值会把新键判成已存在 -> 发 OP_CHANGE -> 客户端 oldVal 为 nil 崩溃
    #   （frame_guard 当场抓到：[_changeSubProperty] CharacterPersistentDict[9002] 不存在）。
    if want is not None and want not in bag:
        tx.set(("characterBag", want), factory.make_character(props, want))
        _log.info("[protagonist] 创建主角 %s", want)

    # 3) choosen + 编队 + 下标
    if want is not None:
        if int(props.get("character_choosen_char") or 0) != want:
            tx.set(("character_choosen_char",), want)
        if int(props.get("choosen_index") or 0) != 1:
            tx.set(("choosen_index",), 1)
        _set_team_hero(tx, props, want)
    elif int(props.get("character_choosen_char") or 0) != 0:
        # 未选性别：开局状态必须是「没有角色」
        tx.set(("character_choosen_char",), 0)

    frames.extend(tx.frames())
    if syncs is not None:
        syncs.extend(frames)
    return frames


def ensure(st: Any, gender: Any, syncs: list[bytes] | None = None) -> list[bytes]:  # noqa: ANN401
    """PlayerState 包装：改完标记 characterBag / character_list 脏（供落盘/同步）。"""
    props = st.props
    bag_before = props.get("characterBag")
    list_before = props.get("character_list")
    frames = ensure_props(props, gender, syncs)
    if props.get("characterBag") is not bag_before:
        st.set("characterBag", props.get("characterBag"))
    if props.get("character_list") is not list_before:
        st.set("character_list", props.get("character_list"))
    return frames
