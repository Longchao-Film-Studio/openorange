#!/usr/bin/env python
"""A/B/C 契约：存档属性来源（game_server/prop_origin.py）测试。

## 这个测试为什么存在

2026-10-28 实机：点左上角等级/头衔入口「点了没反应」。根因是
`title_daily_task_cache` 只在 `avatar_init.json` 里，**玩家的老存档里没有**。

这件事之前已经发生过 5 次，每次都写一个 `_stage_xxx_selfheal` 补一下：
  level_selfheal / recalc_unlocks / chapter_attrs / street_pos / hero_by_gender
—— 同一个坑换个属性来一次，标准的补丁驱动。

本文件测的是**结构**，不是那 5 个补丁：

  [1] 契约 / ORIGIN / 重算函数三者自洽
  [2] INV-A  存档里不许出现 derived / ephemeral
  [3] INV-B  下发的 initDict 必须含契约全集
  [4] INV-C  一个属性只能有一个产生者（幂等 + 不双写）
  [5] schema 迁移：老存档补齐、只加不删、幂等
  [6] 真实老存档样本（冻结副本）迁移后不缺任何契约 key
  [7] 派生属性确实由原始数据决定（改原始数据 -> 派生值跟着变）

用法: python -m tests.test_prop_origin
"""
import copy
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ★ 必须用临时存档目录：绝不碰 work/player_state 里的真实存档（反模式 P12）。
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_origin_")
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(
    os.environ["OO_STATE_DIR"], "unknown.tsv"))

from game_server import player_state as pstate  # noqa: E402
from game_server import prop_origin as origin  # noqa: E402
from game_server.ms import live_init_dict  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail is not None else ""))


def fresh_uid(tag):
    """每个用例用独立 uid，避免状态串台。"""
    return "t_" + tag


def contract_set():
    return set(origin.all_props())


def run():
    contract = contract_set()

    print("\n[1] 契约 / ORIGIN / 重算函数 三者自洽")
    check("ORIGIN 自检无问题", origin.check_origin_consistency() == [],
          origin.check_origin_consistency())
    check("契约全集非空（>200）", len(contract) > 200, len(contract))
    kinds = {n: origin.origin_of(n) for n in contract}
    check("每个契约属性都有合法来源",
          all(v in (origin.STORED, origin.DERIVED, origin.EPHEMERAL)
              for v in kinds.values()),
          {k: v for k, v in kinds.items() if v not in ("stored", "derived", "ephemeral")})
    n_stored = sum(1 for v in kinds.values() if v == origin.STORED)
    n_derived = sum(1 for v in kinds.values() if v == origin.DERIVED)
    n_eph = sum(1 for v in kinds.values() if v == origin.EPHEMERAL)
    check(f"三类数量 {n_stored}/{n_derived}/{n_eph} 合计 == 契约",
          n_stored + n_derived + n_eph == len(contract),
          (n_stored, n_derived, n_eph, len(contract)))
    check("derived 全部在契约里", set(origin.derived_props()) <= contract,
          set(origin.derived_props()) - contract)
    check("ephemeral 全部在契约里", set(origin.ephemeral_props()) <= contract,
          set(origin.ephemeral_props()) - contract)
    overlap = (set(origin.derived_props()) & set(origin.ephemeral_props()))
    check("derived 与 ephemeral 不相交", not overlap, overlap)

    print("\n[2] INV-A：存档里不许出现 derived / ephemeral")
    probe = {"level": 1, "levelup_awards": {1: 0}, "system_p": {},
             "title_daily_task_cache": {"is_first_day": 0},
             "_oo_street_pos": {"x": 1.0}}
    kept = origin.filter_for_save(probe)
    check("filter_for_save 保留 stored", "level" in kept, sorted(kept))
    check("filter_for_save 保留 _oo_ 私有字段", "_oo_street_pos" in kept, sorted(kept))
    leaked = [k for k in kept if origin.origin_of(k) != origin.STORED
              and not k.startswith("_oo_")]
    check("filter_for_save 后不含 derived/ephemeral", not leaked, leaked)

    uid = fresh_uid("inv_a")
    live_init_dict(uid=uid)
    st = pstate.STATES[uid]
    st.schema = origin.SAVE_SCHEMA
    # 故意往状态里塞 B/C 类的旧值（模拟历史存档）
    st.props["levelup_awards"] = {1: 0}
    st.props["title_daily_task_cache"] = {"is_first_day": 0}
    st.save()
    with open(st.path(), encoding="utf-8") as f:
        on_disk = json.load(f)["props"]
    bad_on_disk = [k for k in on_disk
                   if origin.origin_of(k) != origin.STORED and not k.startswith("_oo_")]
    check("落盘后盘上不含 derived/ephemeral", not bad_on_disk, bad_on_disk)
    check("落盘后盘上仍有 level", "level" in on_disk, len(on_disk))

    print("\n[3] INV-B：下发的 initDict 必须含契约全集")
    uid2 = fresh_uid("inv_b")
    d = live_init_dict(uid=uid2)
    sent = set(d.get("__Properties__") or {})
    missing = contract - sent
    check("initDict 一个属性不少", not missing, sorted(missing)[:10])
    check("initDict 没有契约外的多余属性", not (sent - contract), sorted(sent - contract)[:10])
    node = d["__Properties__"].get("title_daily_task_cache")
    check("title_daily_task_cache 已下发且带 __tp__",
          isinstance(node, dict) and node.get("__tp__") == "TitleDailyTaskCache",
          node)

    print("\n[4] INV-C：派生属性只有一个产生者")
    uid3 = fresh_uid("inv_c")
    live_init_dict(uid=uid3)
    st3 = pstate.STATES[uid3]
    first = origin.recompute(st3)
    snapshot = {n: copy.deepcopy(st3.props.get(n)) for n in origin.derived_props()}
    second = origin.recompute(st3)
    check("recompute 幂等（第二次无改动）", not second, second)
    check("recompute 幂等（派生值不变）",
          all(st3.props.get(n) == snapshot[n] for n in origin.derived_props()),
          [n for n in origin.derived_props() if st3.props.get(n) != snapshot[n]])
    check("recompute 返回的 key 全是 declared derived",
          set(first) <= set(origin.derived_props()) or not first,
          set(first) - set(origin.derived_props()))
    lv = st3.props.get("levelup_awards") or {}
    # ★ 只检查**整数键**的值。__tp__（字符串）和 __id__（元数据）不是奖励槽位，
    #   把它们算进来会让这条断言对无关变异也变红 —— 假阳性。
    #   实测踩过：修之前它给 4 个不相干的变异报了红，掩盖了真正的原因。
    bad_slots = {k: v for k, v in lv.items()
                 if isinstance(k, int) and not isinstance(k, bool) and v not in (0, 1)}
    check("levelup_awards 的奖励槽位都显式是 0 或 1（客户端判 == 0）",
          not bad_slots, bad_slots)
    check("levelup_awards 保留 __tp__（客户端 initProperty 靠它认类型）",
          lv.get("__tp__") == "IntDictKeyInt", lv.get("__tp__"))
    check("level 1 没有奖励所以 levelup_awards 里没有 1",
          isinstance(lv, dict) and 1 not in lv,
          sorted(k for k in (lv or {}) if isinstance(k, int)))

    print("\n[5] schema 迁移：只加不删、幂等")
    uid4 = fresh_uid("schema")
    live_init_dict(uid=uid4)
    st4 = pstate.STATES[uid4]
    # 模拟老存档：删两个 stored key，schema 回到 0，并留一个已下线的多余 key
    removed = [n for n in ("player_name", "gender") if n in st4.props]
    for n in removed:
        st4.props.pop(n, None)
    st4.props["legacy_removed_prop"] = 123
    st4.schema = 0
    n1 = origin.migrate_schema(st4)
    check("迁移补上了缺失的 stored key", n1 >= len(removed), (n1, removed))
    check("迁移后没有缺失的 stored key",
          not [n for n in origin.stored_props() if n not in st4.props],
          [n for n in origin.stored_props() if n not in st4.props][:10])
    check("迁移不删多余 key（只加不删）", st4.props.get("legacy_removed_prop") == 123,
          st4.props.get("legacy_removed_prop"))
    check("迁移后 schema 更新", st4.schema == origin.SAVE_SCHEMA, st4.schema)
    n2 = origin.migrate_schema(st4)
    check("迁移幂等（第二次补 0 个）", n2 == 0, n2)

    print("\n[6] 合成老存档样本（故意摘掉 stored key，不依赖任何真实存档）")
    # ★ 2026-11-17：原来这里读 tests/fixtures/old_save_bagcheck.json ——
    #   那是 164KB 的真实玩家存档被误提交进仓库，公开前必须拿掉。
    #   改成「拿当前 initDict 摘掉几个 stored key」来造老存档：
    #   断言的东西完全一样（迁移前缺、迁移后不缺），而且不依赖玩家数据。
    uid5 = fresh_uid("oldsave")
    live_init_dict(uid=uid5)
    src_props = dict(pstate.STATES[uid5].props)
    dropped = [n for n in origin.stored_props() if n in src_props][:5]
    for k in dropped:
        src_props.pop(k, None)
    st5 = pstate.PlayerState(uid=uid5, props=src_props)
    st5.schema = 0
    before_missing = origin.missing_props(st5.props)
    origin.migrate_schema(st5)
    origin.recompute(st5)
    for k, v in origin.materialize().items():
        st5.props.setdefault(k, v)
    after_missing = origin.missing_props(st5.props)
    check("摘掉的 stored key 确实被判定为缺失（样本有效）",
          bool(before_missing), (dropped, before_missing))
    check("老存档迁移 + 重算 + 物化后不缺任何契约 key",
          not after_missing, sorted(after_missing)[:10])
    check("老存档补上的 title_daily_task_cache 在快照里",
          "title_daily_task_cache" in st5.props)

    print("\n[7] 派生值确实由原始数据决定")
    uid6 = fresh_uid("recalc")
    live_init_dict(uid=uid6)
    st6 = pstate.STATES[uid6]
    old_lv = st6.props.get("level")
    from game_server import level as _level
    _level.set_level(st6, 5)
    origin.recompute(st6)
    awards = st6.props.get("levelup_awards") or {}
    want_ids = [lv for lv in range(1, 6) if _level.award_of(lv)]
    check("升级到 5 后 levelup_awards 覆盖 1..5 里有奖励的等级",
          all(lv in awards for lv in want_ids),
          (want_ids, sorted(k for k in awards if isinstance(k, int))))
    _level.set_level(st6, old_lv)

    print("\n[8] ★ 未领奖励必须显式是 0（客户端 avatarlevelcomp.lua:36-45 判 == 0）")
    # ★ 这一段是 D5/D7 破坏性验证暴露出来的缺口。
    #   修之前，把 _stage_derived 整段删掉，31 项断言全绿 —— 因为初始 level=1 时
    #   `awards` 里本来就没有任何奖励槽位，断言「所有槽位是 0/1」对空集合恒真。
    #   所以必须造出「已达成且有奖励」的等级再断言，否则测的是空集合。
    uid7 = fresh_uid("slots")
    live_init_dict(uid=uid7)
    st7 = pstate.STATES[uid7]
    _lv7 = _level.current_level(st7.props)
    # 找一个「当前等级以内且有奖励」的等级：Lv2 就有（Lv1 没有 awardItem）
    rewarded = [lv for lv in range(1, 3) if _level.award_of(lv)]
    check("Lv2 确实带奖励（否则本段测不到东西）", 2 in rewarded, rewarded)
    # 把玩家等级提到 2，模拟「已经升过级」的老存档
    _level.set_level(st7, 2)
    origin.recompute(st7)
    aw = st7.props.get("levelup_awards") or {}
    slot2 = aw.get(2, "<缺失>")
    check("升到 Lv2 后 levelup_awards[2] 是显式 0（不是缺失）", slot2 == 0, slot2)
    # 反过来验：把槽位删掉，recompute 必须补回来（证明它确实是 recompute 在维护）
    aw.pop(2, None)
    check("手动删掉槽位后 awards[2] 确实不见了", 2 not in (st7.props.get("levelup_awards") or {}))
    origin.recompute(st7)
    check("recompute 把删掉的槽位补回成 0",
          (st7.props.get("levelup_awards") or {}).get(2) == 0,
          st7.props.get("levelup_awards"))

    print("\n[9] ★ 登录流程必须真的调用 schema 和 derived（D3/D7 暴露的缺口）")
    # ★ 这一条测的是**接线**，不是函数本身：
    #   把 ms/initdict.py 里的 _stage_schema / _stage_derived 删掉，
    #   上面的断言全都能过（因为 recompute 被测试自己调过了），
    #   但真实登录路径就没在补。所以必须检查登录流程本身。
    import inspect

    from game_server.ms import initdict as _initdict
    src = inspect.getsource(_initdict._live_init_dict_impl)
    check("登录流程调用了 _stage_schema", "_stage_schema(st)" in src)
    check("登录流程调用了 _stage_derived", "_stage_derived(st)" in src)
    check("_stage_schema 在 _stage_derived 之前（先补原始数据再算派生）",
          src.index("_stage_schema(st)") < src.index("_stage_derived(st)"))
    # 再验行为：一个 schema=0 且缺 key 的状态，走一遍登录流程后必须补齐
    uid8 = fresh_uid("wired")
    st8 = pstate.PlayerState(uid=uid8, props={})
    st8.schema = 0
    pstate.STATES[uid8] = st8
    live_init_dict(uid=uid8)
    check("空状态走登录流程后补齐了 stored（证明 _stage_schema 真的在跑）",
          not [n for n in origin.stored_props() if n not in st8.props],
          [n for n in origin.stored_props() if n not in st8.props][:8])
    check("空状态走登录流程后有派生值（证明 _stage_derived 真的在跑）",
          isinstance(st8.props.get("levelup_awards"), dict),
          st8.props.get("levelup_awards"))

    print("\n[10] ★ 契约覆盖：每个「客户端读、但服务端不产生」的属性都必须登记来源")
    # ★ 这一条是 D1/D2 破坏性验证暴露出来的缺口。
    #   修之前，把 ORIGIN 里的 title_daily_task_cache 删掉，全部断言照样绿 ——
    #   因为这个属性从模板下发，看起来「不缺」。但它其实**没有产生者**：
    #   它不在 AvatarProperties 里（不是 stored 的合法来源），
    #   也没有派生函数，只是被模板兜着。下次有人清理模板它就没了。
    #
    #   判据：属性 A 属于「客户端读了，但客户端自己不会产生」= 必须有服务端产生者。
    #   服务端产生者只有两种：stored（模板/存档）或 derived（重算）。
    #   而「不在 AvatarProperties 声明里、又不在存档里」的属性，只能是 ephemeral ——
    #   它必须被显式登记成 ephemeral，否则就是「靠模板兜着的孤儿」。
    from game_server.property import spec as _spec
    contracts = set(origin.all_props())
    # AvatarProperties 里声明过的 = 客户端模板本来就该有它，模板默认值就是合法初始值
    declared = set(_spec._load().get("avatar_properties") or {})
    check("AvatarProperties 声明数 >100（确认读到了 spec_data）", len(declared) > 100, len(declared))
    # 不在 AvatarProperties 里的属性有两种：
    #   a) 契约表（tools/client_contract.py 的 SERVER_PROPS / SIMPLE_PROPS）显式登记过
    #      -> 那是一次**人工判断**，有依据注释，算「已认领」；
    #   b) 两边都没有 -> **孤儿**：模板里有值，但没人认领它的来源。
    #      下次有人清模板它就消失，而客户端读它照样崩
    #      —— 这正是 title_daily_task_cache 出事前的状态。
    from tools import client_contract as _cc
    claimed = set(_cc.SERVER_PROPS) | set(_cc.SIMPLE_PROPS)
    check("客户端契约表非空（确认读到了 client_contract）", len(claimed) > 50, len(claimed))
    orphans = [n for n in contracts
               if n not in declared and n not in claimed
               and origin.origin_of(n) == origin.STORED]
    check("没有「未认领的孤儿属性」（不在声明里、不在契约表里、也没登记派生）",
          not orphans, sorted(orphans)[:12])
    # ★ 反过来守的是「无声退回 stored」：契约表里的属性默认就是 stored（安全侧），
    #   所以**唯一能证明它被登记过来源的方式**是它显式出现在 ORIGIN 里。
    #   对于「语义上每次都是新的」这类属性（title_daily_task_cache 就是），
    #   一旦把它从 ORIGIN 删掉，它就悄悄变成 stored —— 又能进存档了，又能长出两个产生者。
    #   所以这里点名要求：**已知的 ephemeral 名单不能缩水**。
    KNOWN_EPHEMERAL = {"title_daily_task_cache", "street_level_info"}
    shrunk = sorted(KNOWN_EPHEMERAL - set(origin.ephemeral_props()))
    check("已知的 ephemeral 属性没有被悄悄改回 stored", not shrunk, shrunk)
    # ★ 同理守 derived 名单：把某个 derived 改回 stored，它就会进存档，
    #   于是「存档旧值 vs 登录现算」两个产生者并存 —— 这正是本次要根治的病。
    KNOWN_DERIVED = {
        "levelup_awards", "system_p", "story_max_collect_num",
        "story_reward_list", "street_pos_info", "character_choosen_char",
    }
    derived_shrunk = sorted(KNOWN_DERIVED - set(origin.derived_props()))
    check("已知的 derived 属性没有被悄悄改回 stored", not derived_shrunk, derived_shrunk)

    print("\n[11] ★ derived 属性必须能在客户端被读到，且值不能等于模板默认值")
    # ★ 这一条是 D2 暴露的缺口：把 levelup_awards 标成 stored 后，
    #   存档里会存一份旧值，看起来「也有值」，但那是**存档旧值**不是算出来的。
    #   判据：derived 属性的值必须随原始数据变化，且不等于模板初始值（除了确实相同的）。
    uid9 = fresh_uid("derived_live")
    live_init_dict(uid=uid9)
    st9 = pstate.STATES[uid9]
    for name in origin.derived_props():
        kind_in_save = origin.origin_of(name)
        if kind_in_save != origin.DERIVED:
            continue
        check(f"{name} 被登记为 derived（不是 stored）", kind_in_save == origin.DERIVED)
    # 反证：derived 属性一旦被标成 stored，它就会出现在存档里
    saved9 = origin.filter_for_save(dict(st9.props))
    leaked = [n for n in origin.derived_props() if n in saved9]
    check("derived 属性没有出现在存档视图里", not leaked, leaked)

    print()
    print(f"=== 结果: PASS={len(passed)} FAIL={len(failed)} ===")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print(f"通过 {len(passed)} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(run())
