#!/usr/bin/env python3
"""批次 A 三个事件：Consume / GiftFavor / FavorAddLevel。

## 这三个事件补的是什么

服务端这三条 RPC 一直在跑，但从来没发过事件，于是这些任务永远推不动：

  Consume         TaskData 5101303 / 5102605「消耗100中浦币」    (times=[100])
  GiftFavor       TaskData 5000005「赠送1次好感度道具」           (每日重置)
  FavorAddLevel   TaskData 5101806/5103403/5104504/5104606「好感度达到 N 级」

## 关键设计：amount 是保留键

进度必须能按**数量**累加：任务「消耗100中浦币」TriggerTimes=[100]，
客户端显示 sum(doing_targets)/TriggerTimes（node_challenge_task_itemgroup.lua:76-80）。
「用 100 个 × 1 次」和「用 1 个 × 100 次」都要能到 100/100。

累加量放在 params["amount"] 而不是 params["count"]，因为 **count 在
TaskEventData 里是匹配键**（11 条配置带它，如 AddFriend{count:10}），
event_matches 要求相等 —— 拿它当累加量会同时改掉匹配。
amount 在全部 1288 条 TaskEventData 里从未出现，[2] 守住这条不变量。

## 破坏性验证

  把 events.ev_consume 的 "amount" 改回 "count"        -> [3] 红（进度只 +1）
  把 tasks.evaluate_event 的 _inc 固定为 1              -> [3] 红
  把 _add_char_favor_exp 里的 GiftFavor 事件删掉        -> [9] 红
  把 _use_item 里的 Consume 事件删掉                    -> [8] 红
"""
import copy
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="oo_batcha_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_tmp, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_tmp, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_tmp, "cap"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_server import dispatch, gamedata, tasks  # noqa: E402
from game_server import ms as msmod
from game_server import player_state as ps
from game_server.property.bag import grants, special  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402
from game_server.story import events as EV  # noqa: E402

_PASSED: list[str] = []
_FAILED: list[str] = []

CONSUME_TASK = 5101303      # 消耗100中浦币  times=[100]  eventId=530301 itemId=132
GIFT_TASK = 5000005         # 赠送1次好感度道具 times=[1] eventId=1004
FAVOR_TASK = 5101806        # 提升1名龙胤好感度到2级 times=[1] eventId=12002 new_level=2

FAVOR_ITEM = 81001          # 欧嘶卡小金人 CharFavorExp=500
CONSUME_ITEM = 132          # 中浦币


def check(name, cond, got=None):
    if cond:
        _PASSED.append(name)
        print(f"  OK   {name}")
    else:
        _FAILED.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def _fresh(uid):
    props = msmod.conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    ps.reset_state(props, uid=uid)
    msmod.live_init_dict(uid)
    return ps.get_state(uid=uid)


def _start(st, tid):
    """强制把任务置为进行中（跳过播种的前置检查，测试专用）。"""
    tasks.start_task(st, tid, 1)
    return tasks.task_state(st.props, tid)


def _dt(st, tid, eid):
    bag = st.props.get("task_bag") or {}
    t = tasks.task_type_of(tid)
    slot = bag.get(t) if t is not None else None
    if not isinstance(slot, dict):
        return 0
    item = slot.get(tid)
    if not isinstance(item, dict):
        return 0
    return int((item.get("doing_targets") or {}).get(eid) or 0)


def run():  # noqa: C901,PLR0915 —— 一个测试文件一条主线，段落就是验收点
    print("[1] 三个构造器的参数形状")
    e = EV.ev_consume(CONSUME_ITEM, 100)
    check("ev_consume 类型", e.type == EV.EV_CONSUME, e.type)
    check("ev_consume itemId", e.params.get("itemId") == CONSUME_ITEM, e.params)
    check("ev_consume amount=100", e.params.get("amount") == 100, e.params)
    check("ev_consume 不带 event_id（不能去重）", e.event_id is None, e.event_id)

    g = EV.ev_gift_favor(1001, 1)
    check("ev_gift_favor 类型", g.type == EV.EV_GIFT_FAVOR, g.type)
    check("ev_gift_favor 不带 event_id", g.event_id is None, g.event_id)

    f = EV.ev_favor_add_level(1001, 8)
    check("ev_favor_add_level 类型", f.type == EV.EV_FAVOR_ADD_LEVEL, f.type)
    check("ev_favor_add_level new_level", f.params.get("new_level") == 8, f.params)
    check("ev_favor_add_level charId", f.params.get("charId") == 1001, f.params)
    check("ev_favor_add_level 不带 event_id（不同角色分别计数）",
          f.event_id is None, f.event_id)

    print()
    print("[2] ★ amount 是保留键：TaskEventData 里绝不许出现")
    table = gamedata.table("TaskEventData") or {}
    bad = []
    for k, v in table.items():
        if not isinstance(v, dict):
            continue
        p = v.get("EventParam") or {}
        if "amount" in p:
            bad.append((k, v.get("EventType")))
    check("1288 条 TaskEventData 都没有 amount 键",
          not bad, bad[:5])
    check("表里确实有 count（说明为什么不能借用 count）",
          sum(1 for v in table.values()
              if isinstance(v, dict) and "count" in (v.get("EventParam") or {})) > 0)

    print()
    print("[3] Consume：一次用掉 100 个 -> 进度直接到 100 -> 任务完成")
    st = _fresh("t_consume1")
    check("任务置为 Doing", _start(st, CONSUME_TASK) == tasks.TASK_STATE_DOING)
    before = _dt(st, CONSUME_TASK, 530301)
    EV.apply_event(st, EV.ev_consume(CONSUME_ITEM, 100))
    after = _dt(st, CONSUME_TASK, 530301)
    check("进度 0 -> 100", before == 0 and after == 100, (before, after))
    check("任务进 WaitDone（头衔挑战要玩家领奖）",
          tasks.task_state(st.props, CONSUME_TASK) == tasks.TASK_STATE_WAIT_DONE,
          tasks.task_state(st.props, CONSUME_TASK))

    print()
    print("[4] Consume：用 1 个 × 100 次 也到 100")
    st2 = _fresh("t_consume2")
    _start(st2, CONSUME_TASK)
    for _ in range(100):
        EV.apply_event(st2, EV.ev_consume(CONSUME_ITEM, 1))
    check("100 次每次 1 个 -> 进度 100", _dt(st2, CONSUME_TASK, 530301) == 100,
          _dt(st2, CONSUME_TASK, 530301))
    check("同样完成", tasks.task_state(st2.props, CONSUME_TASK) == tasks.TASK_STATE_WAIT_DONE,
          tasks.task_state(st2.props, CONSUME_TASK))

    print()
    print("[5] Consume：itemId 不匹配就不计数")
    st3 = _fresh("t_consume3")
    _start(st3, CONSUME_TASK)
    EV.apply_event(st3, EV.ev_consume(999, 100))
    check("用别的道具 -> 进度仍是 0", _dt(st3, CONSUME_TASK, 530301) == 0,
          _dt(st3, CONSUME_TASK, 530301))

    print()
    print("[6] GiftFavor：发一次就命中（EventParam 为空 -> 任意匹配）")
    st4 = _fresh("t_gift")
    _start(st4, GIFT_TASK)
    check("起始 0", _dt(st4, GIFT_TASK, 1004) == 0)
    EV.apply_event(st4, EV.ev_gift_favor(1001, 1))
    check("发 1 次 -> 进度 1", _dt(st4, GIFT_TASK, 1004) == 1, _dt(st4, GIFT_TASK, 1004))
    check("任务完成", tasks.task_state(st4.props, GIFT_TASK) == tasks.TASK_STATE_WAIT_DONE,
          tasks.task_state(st4.props, GIFT_TASK))

    print()
    print("[7] FavorAddLevel：等级对得上才计数")
    st5 = _fresh("t_favor")
    _start(st5, FAVOR_TASK)
    EV.apply_event(st5, EV.ev_favor_add_level(1001, 1))
    check("发 new_level=1 -> 进度仍 0（任务要的是 2 级）",
          _dt(st5, FAVOR_TASK, 12002) == 0, _dt(st5, FAVOR_TASK, 12002))
    EV.apply_event(st5, EV.ev_favor_add_level(1001, 2))
    check("发 new_level=2 -> 进度 1", _dt(st5, FAVOR_TASK, 12002) == 1,
          _dt(st5, FAVOR_TASK, 12002))
    check("任务完成", tasks.task_state(st5.props, FAVOR_TASK) == tasks.TASK_STATE_WAIT_DONE,
          tasks.task_state(st5.props, FAVOR_TASK))

    print()
    print("[8] ★ 端到端：UseItem_CS 真的发 Consume（用事件捕获验证接线）")
    # ★ 为什么不用中浦币(132)：它 useType=None，是纯货币，走不了 UseItem_CS。
    #   实测 5 个 Consume 道具（132/124/122/123/116）全都是 useType=None ——
    #   它们是货币/材料，不是「可使用道具」。所以「消耗100中浦币」要推进，
    #   必须有一个花中浦币的玩法（商店/兑换/声望升级），当前服务端没有。
    #   这里改用真的能用的道具验证接线本身是否正确。
    USE_ITEM = 715            # 菁华之种·阳，useType="item"
    st6 = _fresh("t_useitem")
    tx = Tx(st6.props)
    grants.grant(st6.props, USE_ITEM, 5, tx=tx, source="test")
    st6.save()
    check("道具已入包", _has_item(st6, USE_ITEM, 5))

    captured = []
    _orig = EV.apply_event

    def _spy(st_, ev, now=None):
        captured.append(ev)
        return _orig(st_, ev, now)

    EV.apply_event = _spy
    try:
        frames = dispatch._use_item(
            9, msmod.mp_arr([USE_ITEM, 5, {}]), uid="t_useitem")
    finally:
        EV.apply_event = _orig
    check("_use_item 有返回帧", bool(frames), len(frames or []))
    check("道具被扣光", not _has_item(st6, USE_ITEM, 1))
    kinds = [x.type for x in captured]
    check("发了 Consume 事件", EV.EV_CONSUME in kinds, kinds)
    ce = next((x for x in captured if x.type == EV.EV_CONSUME), None)
    check("Consume 带对 itemId", ce is not None and ce.params.get("itemId") == USE_ITEM,
          ce.params if ce else None)
    check("Consume 带对 amount", ce is not None and ce.params.get("amount") == 5,
          ce.params if ce else None)
    check("中浦币(132)确实没有 useType（那 2 条任务仍推不动）",
          not gamedata.item_use_type(132), gamedata.item_use_type(132))

    print("[9] ★ 端到端：AddCharFavorExp_CS 真的发 GiftFavor + FavorAddLevel")
    st7 = _fresh("t_favor_gift")
    _start(st7, GIFT_TASK)
    _start(st7, FAVOR_TASK)
    tx7 = Tx(st7.props)
    grants.grant(st7.props, FAVOR_ITEM, 1, tx=tx7, source="test")
    st7.save()
    check("送礼前好感等级 = 1", special.favor_level(st7.props, 1001) == 1,
          special.favor_level(st7.props, 1001))
    dispatch._add_char_favor_exp(
        9, msmod.mp_arr([1001, {str(FAVOR_ITEM): 1}]), uid="t_favor_gift")
    check("送礼后好感等级 = 2", special.favor_level(st7.props, 1001) == 2,
          special.favor_level(st7.props, 1001))
    check("GiftFavor 进度 = 1", _dt(st7, GIFT_TASK, 1004) == 1, _dt(st7, GIFT_TASK, 1004))
    check("FavorAddLevel 进度 = 1", _dt(st7, FAVOR_TASK, 12002) == 1,
          _dt(st7, FAVOR_TASK, 12002))

    print()
    print("[10] favor_level 与客户端 GetFavorLevel 逐点对齐")
    # 客户端 avatarfavorspacecomp.lua:346-372；1001 的 CostFavorExp = 500/1300/2500/4100...
    cases = [(0, 1), (499, 1), (500, 2), (1299, 2), (1300, 3), (2499, 3), (2500, 4)]
    for exp, want in cases:
        got = special.favor_level({"favor_exp_map": {1001: exp}}, 1001)
        check(f"exp={exp} -> Lv{want}", got == want, got)
    check("角色不在表里 -> Lv1", special.favor_level({}, 999999) == 1)
    check("纯读：不改 props", _readonly_ok())

    print()
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        print("失败项：" + ", ".join(_FAILED))
    return 0 if not _FAILED else 1


def _has_item(st, item_id, at_least):
    bag = st.props.get("item_bag") or {}
    total = 0
    for k, v in bag.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if int(v.get("itemId") or 0) == item_id:
            total += int(v.get("num") or 0)
    return total >= at_least


def _readonly_ok():
    props = {"favor_exp_map": {1001: 600}}
    snap = copy.deepcopy(props)
    special.favor_level(props, 1001)
    return props == snap


if __name__ == "__main__":
    sys.exit(run())
