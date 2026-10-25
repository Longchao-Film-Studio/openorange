#!/usr/bin/env python
"""P0-C：客户端影子缓存校验测试。

## 这个测试在验什么

现有测试只断言「服务端 props 改对了」，没有一条校验推出去的帧在客户端是否合法。
本测试把服务端产生的每一条 propertySync 帧喂进 ShadowCache（按客户端
puppetpropertycomponent.lua / customdict.lua 的真实语义实现），断言客户端不会崩。

## 覆盖

A. 影子缓存自身能抓出审计列的 4 类崩溃（用合成坏帧验证检测能力）
B. 真实 handler 产出的帧全部安全（切街区 / NPC 交互 / 宝箱 / CD / 声望 /
   通用任务 / 剧情任务 / 副本结算 / 用道具 / Set* 系列）
C. initDict 能被客户端正确初始化（initProperty 不崩）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_shadow_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shadow_cache import ShadowCache

from game_server import config, gamedata, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from game_server.property import ids as ids_mod

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid):
    config.configure_scaffold(unlock_all=False)
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


def new_cache(uid):
    """建一个已初始化好的影子缓存。"""
    d = msmod._live_init_dict(uid)
    sc = ShadowCache()
    sc.init_property(d["__Properties__"])
    return sc


def feed(sc, frames, tag):
    """把帧喂进影子缓存，返回新增的 errors。"""
    before = len(sc.errors)
    for f in frames:
        sc.apply_frame(f)
    new = sc.errors[before:]
    if new:
        print('    [{}] 客户端会崩 {} 处：'.format(tag, len(new)))
        for e in new[:5]:
            print("      " + e[:110])
    return new


def frame(pid, op, name, value=None):
    return msmod.build_property_sync("", pid, op, name, value)


def run():
    print("\n[A] 影子缓存能抓出审计列的崩溃（合成坏帧）")
    st0 = fresh("sc_probe")
    d0 = msmod._live_init_dict("sc_probe")

    def fresh_cache():
        sc = ShadowCache()
        sc.init_property(d0["__Properties__"])
        return sc

    si_id = st0.props["street_info"]["__id__"]
    tb_id = st0.props["task_bag"]["__id__"]

    # A1 新键用 OP_CHANGE（审计实锤）
    sc = fresh_cache()
    sc.apply_frame(frame(si_id, msmod.OP_CHANGE, "99999", {"__id__": 9999}))
    check("抓到「新键用 OP_CHANGE」", bool(sc.errors), sc.errors[:1])

    # A2 OP_ADD 的 propertyId 不在客户端缓存
    sc = fresh_cache()
    sc.apply_frame(frame(1500, msmod.OP_ADD, "x", {"__id__": 1}))
    check("抓到「OP_ADD pid 不在缓存」", bool(sc.errors), sc.errors[:1])

    # A3 OP_DEL 打结构体字段
    sc = fresh_cache()
    sc.apply_frame(frame(si_id, msmod.OP_ADD, "88888", {"__id__": 8888}))
    sc.apply_frame(frame(8888, msmod.OP_DEL, "prestige_level"))
    check("抓到「OP_DEL 结构体字段」", bool(sc.errors), sc.errors[:1])

    # A4 容器子项带 __tp__（TaskBag 槽位，审计 P0-2 实锤）
    sc = fresh_cache()
    sc.apply_frame(frame(tb_id, msmod.OP_ADD, "1",
                         {"__tp__": "TaskBag", "__id__": 9001}))
    check("抓到「容器子项带 __tp__」", bool(sc.errors), sc.errors[:1])

    # A5 重复 __id__（客户端只是 logger:warn + 覆盖缓存，不是崩）
    sc = fresh_cache()
    sc.apply_frame(frame(si_id, msmod.OP_ADD, "66661", {"__id__": 6666}))
    sc.apply_frame(frame(si_id, msmod.OP_ADD, "66662", {"__id__": 6666}))
    rep = [w for w in sc.warnings if "repeated" in w]
    check("抓到「重复 __id__」告警（不是崩溃）", bool(rep), sc.warnings[:1])

    # A6 OP_DEL 不存在的键
    sc = fresh_cache()
    sc.apply_frame(frame(si_id, msmod.OP_DEL, "no_such_key"))
    check("抓到「OP_DEL 不存在键」", bool(sc.errors), sc.errors[:1])

    # A7 子项缺 __id__
    sc = fresh_cache()
    sc.apply_frame(frame(si_id, msmod.OP_ADD, "55555", {"no_id": 1}))
    check("抓到「子项缺 __id__」", bool(sc.errors), sc.errors[:1])

    print("\n[B] initDict 能被客户端正确初始化")
    sc = new_cache("sc_b")
    check("initProperty 不崩", not sc.errors, sc.errors[:2])
    check("缓存里挂上了对象", len(sc.cache) > 100, len(sc.cache))
    tp_warn = [w for w in sc.warnings if "__tp__" in w]
    check("没有嵌套 __tp__ 残留（或已记录为警告）",
          len(tp_warn) <= 3, tp_warn[:2])

    print("\n[C] 真实 handler 产出的帧全部安全")

    # C1 街区五条路径
    fresh("sc_c")
    sc = new_cache("sc_c")
    for i, (rpc, params, tag) in enumerate([
        (87385605, [1011, None], "切街区"),
        (1693572640, [40004, 0, 400041], "NPC 交互"),
        (1398226462, [101], "开宝箱"),
        (338297034, [1011, 5], "CD 选择"),
        (980877859, [101, 1], "领声望奖"),
    ], start=1):
        _n0 = sc.frame_count
        errs = feed(sc, erpc.handle(rpc, msmod.mp_arr(params), i, uid="sc_c"), tag)
        check("{} 帧安全".format(tag), not errs, errs[:2])
        check("{} 确实产生了属性同步帧".format(tag), sc.frame_count > _n0,
              sc.frame_count - _n0)

    # C2 通用任务（槽位 + 任务项）
    fresh("sc_d")
    sc2 = new_cache("sc_d")
    td = gamedata.table("TaskData") or {}
    tid = None
    for k, v in td.items():
        if isinstance(v, dict) and isinstance(v.get("TaskType"), int):
            tid = v.get("id") or int(k)
            break
    check("找到可测的 TaskData", tid is not None, tid)
    errs = feed(sc2, erpc.handle(1744435905, msmod.mp_arr([tid]), 1, uid="sc_d"),
                "接通用任务")
    check("接通用任务帧安全", not errs, errs[:2])
    errs = feed(sc2, erpc.handle(42514969, msmod.mp_arr([tid]), 2, uid="sc_d"),
                "完成通用任务")
    check("完成通用任务帧安全", not errs, errs[:2])

    # C3 剧情任务推进（1000 是 TaskType=3，走事件路径；RPC 只接受 1/2/5）
    st3 = fresh("sc_e")
    sc3 = new_cache("sc_e")
    from game_server.story import events as _sev3
    _r3 = _sev3.apply_event(st3, _sev3.ev_dungeon_finish(1004, 1, 1))
    errs = feed(sc3, erpc._finish_story_tasks(st3, list(_r3.hits)),
                "剧情任务 1000")
    check("剧情任务帧安全", not errs, errs[:2])
    check("任务 1000 在客户端变成 Finished",
          story.story_state(st3.props, 1000) == story.TASK_STATE_FINISHED)

    # C4 副本结算全链路
    fresh("sc_f")
    sc4 = new_cache("sc_f")
    errs = feed(sc4, erpc.handle(
        erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
        msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                       "modeId": 1, "storyID": 1}]), 1, uid="sc_f"), "进副本")
    check("进副本帧安全", not errs, errs[:2])
    # 注：进副本本身只回应答帧、不改属性，所以这里不断言 frame_count
    info = {"toStreetId": 101, "toStreetPosId": 50005, "finishReason": 5,
            "userList": {"sc_f": 1},
            "sc_f": {"win": True, "useTime": 95, "battle_items": {203: 3}}}
    _n4 = sc4.frame_count
    errs = feed(sc4, erpc.handle(
        erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
        msmod.mp_arr([info]), 2, uid="sc_f"), "副本结算")
    check("副本结算帧安全", not errs, errs[:2])
    check("副本结算确实产生了属性同步帧", sc4.frame_count > _n4,
          sc4.frame_count - _n4)

    # C5 Set* 系列
    fresh("sc_g")
    sc5 = new_cache("sc_g")
    for i, (rpc, params) in enumerate([
        (40460550, [1001]),            # SetPortrait_CS
        (2040954396, ["hello"]),       # SetSignature_CS
        (885846933, []),               # SetCanScore_CS
    ], start=1):
        errs = feed(sc5, erpc.handle(rpc, msmod.mp_arr(params), i, uid="sc_g"),
                    "Set* {}".format(rpc))
        check("Set* {} 帧安全".format(rpc), not errs, errs[:2])

    print("\n[D] 全局一致性")
    for uid in ("sc_c", "sc_d", "sc_e", "sc_f", "sc_g"):
        stx = pstate.get_state(uid=uid)
        ok, dup = ids_mod.verify_unique(stx.props)
        check("{} 全 props __id__ 唯一".format(uid), ok, dup)
        bag = stx.props.get("item_bag") or {}
        bad = [k for k in bag if isinstance(k, int) and not isinstance(k, bool)]
        check("{} 背包键全是字符串".format(uid), not bad, bad[:3])

    print("\n[E] ★ RPC 抽样扫描（24/114）：挑能安全调用的 handler 验帧")
    # 用一个全新存档，逐个调 RPC，把产出的属性同步帧喂进影子缓存。
    # 目的不是验业务逻辑（那是各模块测试的事），而是验「帧本身合法」。
    sweep = [
        ("SetPortrait_CS", 40460550, [1001]),
        ("SetPlayerCard_CS", 1015071836, [1]),
        ("SetPlayerCardFrame_CS", 68979491, [1]),
        ("SetPortraitFrame_CS", 473613168, [1]),
        ("SetBackground_CS", 287510254, [1]),
        ("SetChatBubble_CS", 1765824800, [1]),
        ("SetSignature_CS", 2040954396, ["hello"]),
        ("SetIsCharListShown_CS", 1959195014, [True]),
        ("SetIsHonorWallShown_CS", 1213546920, [True]),
        ("SetShownHonorWallCharId_CS", 1408482175, [9001]),
        ("SetChoosenIndex_CS", 1556161069, [1]),
        ("SetChoosenChar_CS", 524477307, [9001]),
        ("SetPlayerTitle_CS", 381810662, [1]),
        ("SetCanScore_CS", 885846933, []),
        ("SetHasScore_CS", 927394016, []),
        ("RetSetHasScore_CS", 84440019, []),
        ("SetCancelScore_CS", 1450356294, []),
        ("Street_Redpoint_Update_CS", 1315049093, [1, 1]),
        ("updateStreetTraceTask_CS", 1390116544, [1032]),
        ("Street_Mainline_Levelup_CS", 484169195, [10101]),
        ("RequestSwitchStreet_CS", 87385605, [1011, None]),
        ("RequestLeaveStreet_CS", 1707898947, [1011]),
        ("RequestEnterStreetSurface_CS", 743475140, [1]),
        ("BattleTeach_Activate_CS", 842596900, [101]),
    ]
    fresh("sc_sweep")
    sc6 = new_cache("sc_sweep")
    bad_rpcs = []
    for i, (name, rpc, params) in enumerate(sweep, start=1):
        try:
            frames = erpc.handle(rpc, msmod.mp_arr(params), i, uid="sc_sweep")
        except Exception as ex:
            bad_rpcs.append("{} handler 抛异常: {}".format(name, ex))
            continue
        errs = feed(sc6, frames, name)
        if errs:
            bad_rpcs.append("{}: {}".format(name, errs[0][:80]))
    check('抽样 {} 个 RPC，全部帧合法'.format(len(sweep)), not bad_rpcs, bad_rpcs[:5])
    check("影子缓存确实处理了帧", sc6.frame_count > 10, sc6.frame_count)
    print('    （处理 {} 条属性同步帧，跳过 {} 条非同步帧）'.format(sc6.frame_count, sc6.skipped))

    print("\n[F] ★ P0-1 回归：接任务后重新登录，initDict 不能带嵌套 __tp__")
    # 审计实测：tasks.ensure_type_slot 把 {"__tp__":"TaskBag"} 写进 state，
    # 之前 _live_init_dict 只过滤顶层 _oo_，嵌套 __tp__ 原样进登录 initDict ->
    # 客户端 initProperty -> TaskBag:roinit 把 "TaskBag" 字符串当子项 ->
    # value.__id__ 对字符串取值 -> 崩。
    stF = fresh("sc_relogin")
    tidF = None
    for _k, _v in (gamedata.table("TaskData") or {}).items():
        if isinstance(_v, dict) and isinstance(_v.get("TaskType"), int):
            tidF = _v.get("id") or int(_k)
            break
    erpc.handle(1744435905, msmod.mp_arr([tidF]), 1, uid="sc_relogin")
    stF.save()
    pstate._STATES.pop("sc_relogin", None)          # 模拟进程重启
    dF = msmod._live_init_dict("sc_relogin")
    tbF = (dF["__Properties__"].get("task_bag") or {})
    check("根属性 task_bag 仍带 __tp__（客户端靠它认类型）",
          tbF.get("__tp__") == "TaskTypeBag", tbF.get("__tp__"))
    slotF = tbF.get(1)
    check("嵌套的 task_bag[1] 已剥掉 __tp__",
          isinstance(slotF, dict) and "__tp__" not in slotF,
          list(slotF.keys()) if isinstance(slotF, dict) else slotF)
    scF = ShadowCache()
    scF.init_property(dF["__Properties__"])
    check("接任务后重登，客户端 initProperty 不崩", not scF.errors, scF.errors[:2])

    # 顺带扫一遍：所有根属性的嵌套值里都不该有 __tp__
    bad_tp = []
    def _scan(o, path):
        if isinstance(o, dict):
            for k2, v2 in o.items():
                if k2 == "__tp__" and path.count(".") > 0:
                    bad_tp.append(path)
                _scan(v2, path + "." + str(k2))
        elif isinstance(o, (list, tuple)):
            for i2, v2 in enumerate(o):
                _scan(v2, path + '[{}]'.format(i2))
    for k2, v2 in dF["__Properties__"].items():
        _scan(v2, k2)
    check("登录 initDict 里没有嵌套 __tp__ 残留", not bad_tp, bad_tp[:5])

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
