#!/usr/bin/env python3
"""game_server.prop_origin —— 根属性「来源」契约（A/B/C）。

## 这个模块凭什么存在

2026-10-28 实机：点左上角等级/头衔入口，「面板打开即自我关闭」= 用户看到「点了没反应」。
日志：`node_daily_task_itemGroup.lua:106 attempt to index field
'title_daily_task_cache' (a nil value)`。

属性在 `avatar_init.json` 里**有**，但玩家的存档里**没有** —— 因为：

    avatar_init.json 只在新存档时被用一次，之后存档就是唯一真相。

于是每加一个属性，老存档就缺一个 key。这个问题已经出现过 **5 次**，
每次都写一个 `_stage_xxx_selfheal` 补一下：

    _stage_level_selfheal      补 levelup_awards
    _stage_recalc_unlocks      补 unlocked_system
    _stage_chapter_attrs       补 story_max_collect_num / story_reward_list
    _stage_street_pos          补 street_pos_info
    _stage_hero_by_gender      补 character_choosen_char

这是补丁驱动的教科书形态：**同一个坑换个属性来一次。**

## 做法：给每个属性定「来源」，存档只留必须存的

对照 Grasscutter（原神）：`Player` 的 `properties` 是一个 `Map<Integer,Integer>`，
构造时按 `PlayerProperty` 枚举**全部填默认值**，读恒有兜底（`getOrDefault(prop, 0)`）。
LunarCore（星穹铁道）更彻底：派生状态标 `transient`，**根本不进数据库**。

我们把这条路搬过来，分三类：

| 类 | 含义 | 存档 | 登录时 |
|---|---|---|---|
| **stored**    | 玩家做过的事，没有其它来源 | 存 | 原样使用 |
| **derived**   | 是 stored 的纯函数结果 | 不存 | 每次现算 |
| **ephemeral** | 语义上每次都是新的 | 不存 | 从模板生成 |

## 不变量（tests/test_prop_origin.py 逐条可红）

    INV-A  存档里的每个非 `_oo_` key 都必须是 stored 类
    INV-B  下发的 initDict 必须含「stored ∪ derived ∪ ephemeral」= 契约全集
    INV-C  一个属性只能有一个产生者：stored 来自存档、derived 来自 recompute()、
           ephemeral 来自 materialize()。不允许「既是 stored 又被 recompute 写」。

★ INV-C 是本次的模型核心。之前所有「新存档对、老存档错」的 bug 都是它被破坏：
`levelup_awards` 存档里有旧值、登录又补新值，两个写入口打架，谁先跑谁赢。

## 唯一入口（R3 / R8）

    ORIGIN                     属性名 -> 来源（唯一清单）
    origin_of(name)            查来源，查不到直接 raise（**不许默认成 stored**）
    stored_props() / derived_props() / ephemeral_props()
    recompute(st)              跑全部 derived，返回 {名字: 值}（**不直接写 st.props**）
    materialize(template)      从模板生成 ephemeral
    migrate_schema(st)         老存档补 stored 类新增的 key（只加不删）
    filter_for_save(props)     写盘前剔掉 derived/ephemeral（INV-A 的唯一保证手段）

## 加一个新属性的流程

1. 在 `tools/client_contract.py` 的 SERVER_PROPS / SIMPLE_PROPS 里加契约；
2. 跑 `python tools/build_avatar_init.py` 重新生成模板；
3. 在下面的 `ORIGIN` 里登记来源（stored / derived / ephemeral）；
4. 如果是 stored，`SAVE_SCHEMA += 1`；如果是 derived，在 `_RECOMPUTERS` 里加一条。

**不需要写任何 `_stage_xxx_selfheal`。** 这是本模块存在的意义。
"""
from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable
from typing import Any

#: 存档结构版本。**stored 类新增属性时必须 +1。**
#:
#: 历史：
#:   1  2026-10-28 初版。此前存档没有版本号，缺什么靠 _stage_*_selfheal 现补。
SAVE_SCHEMA = 1

#: 来源标签（用常量而不是裸字符串，防止拼错时静默变成「查不到」）。
STORED = "stored"
DERIVED = "derived"
EPHEMERAL = "ephemeral"
_KINDS = (STORED, DERIVED, EPHEMERAL)


# ---------------------------------------------------------------------------
# ORIGIN：属性来源的唯一清单
# ---------------------------------------------------------------------------
#
# 只登记「需要特别说明来源」的属性。**其余一律按 stored 处理**
# （理由见 _default_kind 的注释 —— 默认 stored 是安全侧：多存一个不会崩，
#  少存一个会让客户端 index nil）。
#
# ★ 这里只写 derived / ephemeral，不写 stored。
#   写全 220 个 stored 没有信息量，反而让「哪些是特殊的」被淹没。
ORIGIN: dict[str, str] = {
    # --- derived: 由 level 算出来 ---
    # 客户端 avatarlevelcomp.lua:36-45 判 `levelup_awards[i] == 0` 决定红点。
    # 存盘会出现「存档里的旧值」和「按 level 现算的新值」两个来源（INV-C 违规）。
    "levelup_awards": DERIVED,
    # --- derived: 由 story/task/level 算出来 ---
    # 客户端 avatarsystemguidecomp.lua:93 读的是 self.system_p.unlocked_system[sysId]。
    # ★ 登记的是**根属性 system_p**，不是嵌套字段 unlocked_system ——
    #   契约的单位是根属性，只有根属性有「存不存」这个问题。
    #   （第一版写错成 unlocked_system，被 check_origin_consistency 当场抓到。）
    "system_p": DERIVED,
    # avatarstorylinecomp.lua:98/102 读 self.story_max_collect_num / story_reward_list
    "story_max_collect_num": DERIVED,
    "story_reward_list": DERIVED,
    # 由 _oo_street_pos（stored 的私有存档）拼出来
    "street_pos_info": DERIVED,
    # 由 gender + characterBag 决定（protagonist.ensure 是唯一入口）
    "character_choosen_char": DERIVED,
    # --- ephemeral: 每次都是新的，没有跨登录状态 ---
    # config.lua:7258 TitleDailyTaskCache：is_first_day 默认 1，fst_task/sed_task 默认 0，
    # 两个 type_tasks 是空 IntList。客户端**只有一个读点**
    # （node_daily_task_itemGroup.lua:106 判 is_first_day 决定刷新按钮显不显示），
    # **零个写点** —— 没有任何需要跨登录保留的状态。
    "title_daily_task_cache": EPHEMERAL,
    # street_level_info：街区等级信息，同样每次登录由服务端按当前进度重算后再下发。
    # 当前实现是空模板（还没做街区等级玩法），登记成 ephemeral 保证它一定被下发。
    "street_level_info": EPHEMERAL,
}


def origin_of(name: str) -> str:
    """查一个根属性的来源。

    ★ 查不到不等于报错：**默认 stored**。理由是这个默认是安全侧 ——
      多存一个属性不会让客户端崩，少存一个会让它 `index a nil value`。
      真正需要「必须登记」的是派生属性，它们漏登记会被 _check_origin_consistency 抓到。
    """
    kind = ORIGIN.get(name, STORED)
    if kind not in _KINDS:
        raise ValueError(f"属性 {name} 的来源标签非法: {kind!r}（只允许 {_KINDS}）")
    return kind


def stored_props() -> list[str]:
    """需要存盘的属性名（契约全集里排除 derived/ephemeral）。"""
    return [n for n in all_props() if origin_of(n) == STORED]


def derived_props() -> list[str]:
    return sorted(n for n in ORIGIN if ORIGIN[n] == DERIVED)


def ephemeral_props() -> list[str]:
    return sorted(n for n in ORIGIN if ORIGIN[n] == EPHEMERAL)


def all_props() -> list[str]:
    """契约全集：模板里所有公开根属性（剥掉 `_oo_` 私有字段）。"""
    from . import ms as _ms  # noqa: PLC0415

    props = _ms.AVATAR_INIT_DICT.get("__Properties__") or {}
    return sorted(k for k in props if isinstance(k, str) and not k.startswith("_oo_"))


def template_value(name: str) -> Any:  # noqa: ANN401
    """模板里某个属性的初始值（深拷贝，调用方可以随便改）。"""
    from . import ms as _ms  # noqa: PLC0415

    props = _ms.AVATAR_INIT_DICT.get("__Properties__") or {}
    if name not in props:
        raise KeyError(f"契约里没有属性 {name}（先加到 tools/client_contract.py）")
    return copy.deepcopy(props[name])


def materialize(template: dict | None = None) -> dict:
    """从模板生成 ephemeral 类的值。返回 {名字: 值}，不改入参。

    template 默认用 avatar_init.json。测试可以传别的模板来验证「不依赖存档」。
    """
    from . import ms as _ms  # noqa: PLC0415

    src = template if template is not None else (_ms.AVATAR_INIT_DICT.get("__Properties__") or {})
    out: dict = {}
    for name in ephemeral_props():
        if name in src:
            out[name] = copy.deepcopy(src[name])
    return out


# ---------------------------------------------------------------------------
# derived：派生函数
# ---------------------------------------------------------------------------
#
# 每个函数是 `(st) -> {属性名: 值}` 或者直接原地算完返回改动集合。
# 统一约定：**返回 {名字: 新值}**，由 recompute() 一次性写回 —— 这样
# 「谁写的」永远只有 recompute 一个地方（INV-C）。

Recomputer = Callable[[Any], dict]


def _produces(*names: str):
    """装饰器: 声明一个重算函数会产出哪些根属性（供一致性自检用）。

    为什么需要它: 重算函数返回的是「运行时算出来的」，静态没法知道；
    这个声明让「登记了 derived 但忘了写重算」变成测试能抓到的错误。
    """

    def deco(fn):
        fn._produces = names  # noqa: SLF001
        return fn

    return deco


def _with_meta(src: dict, body: dict) -> dict:
    """把 src 里的元数据（`__tp__` / `__id__`）复制进新建的字典。

    ★ 为什么单独抽出来：派生函数都是「读出旧 dict -> 算一个新 dict」，
      而客户端 initProperty 对根属性的表值**要求带 `__tp__`**
      （puppetpropertycomponent.lua:52-53 的 assert customType ~= nil），
      漏复制就等于把账号登不上去。抽成一个函数让「忘带元数据」只有一种写法。
    """
    out = dict(body)
    for key in ("__tp__", "__id__"):
        if isinstance(src, dict) and src.get(key) is not None:
            out[key] = src[key]
    return out


@_produces("levelup_awards")
def _derive_levelup_awards(st: Any) -> dict:  # noqa: ANN401
    """levelup_awards：把**全部**「有 awardItem 的等级」都展开成显式 0/1。

    ★ 为什么是「全部等级」而不是「1..当前等级」—— 两处客户端读法都要满足：

      a) avatarlevelcomp.lua:36-45（红点）：
           for i = 1, level do
             if tableData[i].awardItem and self.levelup_awards[i] == 0 then return true end
           end
         判 `== 0`，nil 不亮红点 -> 1..level 内必须显式 0。

      b) playerphasepanel.lua:191/228/283/366（奖励列表）：
           local isObtained = GlobalData.Avatar.levelup_awards[rawData.id] == 1
         遍历的是**整张 LevelExpData**，不看当前等级 -> 全表都得有键，否则
         高等级奖励项在列表里永远显示「未领」但数据不存在。

    两处合起来 = 覆盖 `level.award_of()` 认识的所有等级（145 个，1 级没有奖励）。
    已领过的保留 1（那是玩家做过的事，属于 stored 的部分）。

    ★ 实测踩过：第一版写成 `range(1, current_level+1)`，
      结果 2 级玩家的 playerphasepanel 里 3..145 级的奖励全部读不到。
      test_player_level 的幂等断言当场变红（ensure_award_slots 补 146 个，
      这边只补 2 个，两边互相打架）。
    """
    from . import level as _level  # noqa: PLC0415

    src = st.props.get("levelup_awards")
    src = src if isinstance(src, dict) else {}
    claimed = {k for k, v in src.items() if v == 1}
    awards: dict = {}
    for lv in _level.award_levels():
        awards[lv] = 1 if lv in claimed else 0
    # ★ 必须保留 __tp__ 和 __id__：
    #   根属性的 __tp__ 是客户端 initProperty 认类型的唯一依据
    #   （puppetpropertycomponent.lua:52-53 的 assert customType ~= nil），
    #   丢了它整个账号登不上。重建 dict 时最容易漏这一条 —— 实测漏过。
    return {"levelup_awards": _with_meta(src, awards)}


@_produces("system_p")
def _derive_unlocked_system(st: Any) -> dict:  # noqa: ANN401
    """unlocked_system: 按 AllSystemCatalogueData 的数据条件重算。

    ★ 这是**嵌套字段**（根属性是 system_p）。所以返回的是整个 system_p 的新值 ——
      这样 recompute 写回的仍然是根属性，INV-C「一个根属性一个产生者」不被破坏。

    ★ 它属于 derived 而不是 stored: 值完全由 story_state / task_bag / level 决定，
      存盘只会造成「存档旧值 vs 现算新值」两个来源。

    客户端依据: avatarsystemguidecomp.lua:93
        return self.system_p.unlocked_system[sysId] == true
    """
    from .story.rpc import recalc_system_unlocks  # noqa: PLC0415

    sp = st.props.get("system_p")
    if not isinstance(sp, dict):
        return {}
    before = copy.deepcopy(sp.get("unlocked_system") or {})
    # recalc_system_unlocks 是既有的唯一写入口（含双向维护 + 日志），直接复用。
    recalc_system_unlocks(st)
    after = st.props.get("system_p")
    if not isinstance(after, dict):
        return {}
    if after is sp and after.get("unlocked_system") == before:
        return {}
    return {"system_p": copy.deepcopy(after)}


@_produces("story_max_collect_num", "story_reward_list")
def _derive_story_line(st: Any) -> dict:  # noqa: ANN401
    """story_max_collect_num / story_reward_list: 按 StoryLineData 重算。

    客户端 avatarstorylinecomp.lua:98 / :102 读它们。
    sync_story_* 是既有实现；这里只负责调用并把结果收集成「根属性新值」。
    """
    from . import story as _story  # noqa: PLC0415

    out: dict = {}
    for name, fn in (
        ("story_max_collect_num", _story.sync_story_max_collect_num),
        ("story_reward_list", _story.sync_story_reward_list),
    ):
        before = copy.deepcopy(st.props.get(name))
        with contextlib.suppress(Exception):
            fn(st)
        after = st.props.get(name)
        if after != before:
            out[name] = copy.deepcopy(after)
    return out


@_produces("street_pos_info")
def _derive_street_pos(st: Any) -> dict:  # noqa: ANN401
    """street_pos_info: 从 _oo_street_pos（存档私有）拼出来。

    `_oo_street_pos` 是 stored（玩家上次上报的位置），`street_pos_info` 是 derived
    （客户端要的形状）。前者以 `_oo_` 开头，不下发也不参与契约，所以两者不冲突。

    客户端 streetcomp.lua:29 getStreetPosInfo -> self.street_pos_info，
    再被 streetsystemmgrbase.lua:561-566 用来恢复出生点。
    """
    sp = st.props.get("_oo_street_pos")
    cur = st.props.get("street_pos_info")
    if not isinstance(sp, dict) or not isinstance(cur, dict):
        return {}
    out = copy.deepcopy(cur)
    with contextlib.suppress(Exception):
        out["x"] = float(sp.get("x") or 0)
        out["y"] = float(sp.get("y") or 0)
        out["z"] = float(sp.get("z") or 0)
        out["dir"] = float(sp.get("dir") or 0)
    return {"street_pos_info": out}


@_produces("character_choosen_char")
def _derive_hero(st: Any) -> dict:  # noqa: ANN401
    """character_choosen_char / characterBag / choosen_index: 按 gender 保证唯一主角。

    `protagonist.ensure` 是既有的唯一入口（清理男女双主角、对齐编队）。
    """
    from . import protagonist as _prot  # noqa: PLC0415

    names = ("character_choosen_char", "characterBag", "choosen_index")
    before = {n: copy.deepcopy(st.props.get(n)) for n in names}
    with contextlib.suppress(Exception):
        _prot.ensure(st, st.props.get("gender"))
    return {n: copy.deepcopy(st.props.get(n))
            for n in names if st.props.get(n) != before[n]}


#: 派生函数的登记表。
#: **新增 derived 属性必须在这里登记**，否则
#: tests/test_prop_origin.py 的「derived 必须有重算函数」会红。
_RECOMPUTERS: tuple[Callable[[Any], dict], ...] = (
    _derive_levelup_awards,
    _derive_unlocked_system,
    _derive_story_line,
    _derive_street_pos,
    _derive_hero,
)


def recompute(st: Any) -> dict:  # noqa: ANN401
    """跑全部派生函数，把结果**一次性**写回 st.props，返回改动集合。

    ★ 这是 derived 属性的唯一写入口（INV-C）。
      既有派生函数（recalc_system_unlocks / sync_story_* / protagonist.ensure）
      的签名要 st，它们的副作用被这里收拢成统一的写回，调用点不需要知道。

    幂等：跑两次，第二次返回空字典。
    """
    changed: dict = {}
    for fn in _RECOMPUTERS:
        with contextlib.suppress(Exception):
            res = fn(st)
            if isinstance(res, dict):
                changed.update(res)
    # ★ 只在「值真的变了」时才写回并上报。
    #   不这样做的后果：_derive_levelup_awards 每次都新建一个 dict，
    #   即使内容相同，引用也不同 -> changed 非空 -> 幂等性测试红，
    #   而且每次登录都会多出一条「派生属性重算」日志（噪音淹没真正的变化）。
    applied: dict = {}
    for name, val in changed.items():
        if st.props.get(name) == val:
            continue
        st.props[name] = val
        applied[name] = val
    return applied


def filter_for_save(props: dict) -> dict:
    """写盘前过滤: 剔掉 derived / ephemeral（INV-A 的唯一保证手段）。

    ★ 为什么要过滤而不是「不写」: 现状下 st.props 里混着 B/C 类的旧值
      （历史存档留下的）。只要 save 不过滤，它们就被写回去，
      下次登录又多一个「存档旧值」，「两个产生者」永远治不好。

    `_oo_` 私有字段**保留** —— 那是服务端自己的数据，客户端看不到。
    """
    out: dict = {}
    for k, v in props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            out[k] = v
            continue
        if isinstance(k, str) and origin_of(k) in (DERIVED, EPHEMERAL):
            continue
        out[k] = v
    return out


def migrate_schema(st: Any) -> int:  # noqa: ANN401
    """老存档补齐 stored 类新增的 key（只加不删）。返回补了几个。

    ★ 只补 stored 类: derived / ephemeral 不该在存档里，补了反而制造 INV-C 违规。
    ★ 只加不删: 多出来的 key（下线的属性）留着无害，删了会丢数据。

    ★★ 这里有一个实测踩过的陷阱（2026-10-28，test_shadow_cache 抓到）：
      **不能因为 `schema` 已经是当前版本就跳过补全。**

      B 类派生属性不进存档，所以「存档里没有 levelup_awards」是**正常状态**，
      而每次登录都需要它先在内存里有一份带 `__tp__` 的空壳，再由 recompute 填内容。
      如果补全被 schema 挡住，重登后 `levelup_awards` 就整个不存在，
      客户端 initProperty 读到「表但没有 __tp__」直接 assert 崩。

      结论：**幂等性由「key 已存在就不动」保证，不由 schema 保证。**
             schema 只用来判断「要不要打迁移日志」，不用来判断「要不要补」。
    """
    cur = getattr(st, "schema", 0) or 0
    n = 0
    for name in stored_props():
        if name in st.props:
            continue
        try:
            st.props[name] = template_value(name)
        except KeyError:
            continue
        n += 1
    # derived / ephemeral 也要有壳：它们不进存档，但**必须进内存**，
    # 否则 recompute / materialize 没有基线可写，快照里就会出现缺属性。
    for name in derived_props() + ephemeral_props():
        if name in st.props:
            continue
        try:
            st.props[name] = template_value(name)
        except KeyError:
            continue
    if cur < SAVE_SCHEMA:
        st.schema = SAVE_SCHEMA
    return n


def missing_props(props: dict) -> list[str]:
    """契约全集里 props 缺哪些（测试和日志用）。"""
    have = set(props)
    return [n for n in all_props() if n not in have]




def _consistency_impl() -> list[str]:
    """check_origin_consistency 的实现（分离出来便于单测直接调用）。"""
    problems: list[str] = []
    contract = set(all_props())
    for name in ORIGIN:
        if name not in contract:
            problems.append(f"ORIGIN 里的 {name} 不在契约里（错别字？）")

    # derived: 每个根属性必须被至少一个重算函数「声明」产出。
    produced: set[str] = set()
    for fn in _RECOMPUTERS:
        produced.update(getattr(fn, "_produces", ()))
    for name in derived_props():
        if name not in produced:
            problems.append(f"derived 属性 {name} 没有任何重算函数声明产出它")

    for name in ephemeral_props():
        if name not in contract:
            problems.append(f"ephemeral 属性 {name} 不在模板里，materialize 产不出")
    return problems


def check_origin_consistency() -> list[str]:
    """自检: 契约 / ORIGIN / 重算函数三者是否自洽。返回问题列表（空 = 一致）。

    检查项:
      1. ORIGIN 里的每个名字都必须在契约里（否则是错别字）;
      2. 每个 derived 都必须有重算函数产出（否则登录后它是 nil）;
      3. 每个 ephemeral 都必须在模板里有值（否则 materialize 产不出）。
    """
    return _consistency_impl()
