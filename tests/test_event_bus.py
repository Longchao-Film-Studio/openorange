#!/usr/bin/env python
"""P0-D：事件总线测试。

## 要验什么

D 项解决的是「服务端只响应 RPC，没有主动推进」：
story.evaluate_event 全仓库只有 1 个调用点（副本结算里写死 FinishDungeon），
所以交道具 / 升等级 / 拿道具 / NPC 交互这些明明做了，剧情任务却纹丝不动。

## 覆盖

A. 结构：apply_event 是唯一写入口
B. event_id 去重：同一 id 发两次只生效一次
C. 事件不经过 RPC 也能推进任务（直接调 apply_event）
D. 4 种事件类型（SubmitItem / AvatarAttr / GetItem / CharData）各能推进对应剧情任务
E. 链式任务（TaskType 6）在事件触发后自动检查
F. 端到端：SubmitItem_CS / RecordItemObtain_CS / UpgradeCharacter_CS
G. 服务端主动推进：登录 tick + pending chain 在第一条 RPC 补完
H. 帧安全：以上路径产出的帧喂进影子缓存不崩
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_evbus_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,

                         config, story, gamedata)

from tests.frame_guard import guarded_handle  # noqa: E402
from game_server.story import events
from shadow_cache import ShadowCache

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

def activate(st, task_id):
    """把剧情任务设为进行中。"""
    story.start_story_task(st, int(task_id), 0)
    return story.story_state(st.props, int(task_id))

def finished(st, task_id):
    return story.story_state(st.props, int(task_id)) == story.TASK_STATE_FINISHED

def run():
    print("\n[A] 结构：apply_event 是唯一写入口")
    check("GameEvent 有 type/params/source/event_id",
          all(hasattr(events.GameEvent("x"), a)
              for a in ("type", "params", "source", "event_id")))
    check("apply_event 存在且可调用", callable(events.apply_event))
    check("tick 存在（服务端主动推进入口）", callable(events.tick))
    check("CORE_TYPES 覆盖 4 种",
          set(events.CORE_TYPES) == {"SubmitItem", "AvatarAttr",
                                     "GetItem", "CharData"},
          events.CORE_TYPES)
    # 全仓只剩 1 个 evaluate_event 调用点（在 events.py 里）
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for dirpath, _d, files in os.walk(os.path.join(root, "game_server")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            txt = open(p, encoding="utf-8", errors="replace").read()
            # 排除 def evaluate_event( 这个定义行
            for ln in txt.split("\n"):
                if "evaluate_event(" in ln and not ln.lstrip().startswith("def "):
                    hits.append(os.path.relpath(p, root))
                    break
    check("evaluate_event 只在 events.py 里被调用",
          hits == [os.path.join("game_server", "story", "events.py")], hits)

    print("\n[B] event_id 去重")
    st = fresh("ev_dedup")
    activate(st, 50290)
    e = events.ev_street_interact(40004, 400041)   # 带 event_id
    r1 = events.apply_event(st, e)
    check("第一次不重复", not r1.deduped)
    r2 = events.apply_event(st, e)
    check("第二次被去重", r2.deduped, r2)
    check("去重后不再产生 hits", not r2.hits, r2.hits)
    # 不带 event_id 的事件不去重
    e2 = events.ev_submit_item(1, 1)
    r3 = events.apply_event(st, e2)
    r4 = events.apply_event(st, e2)
    check("不带 event_id 的事件不去重", not r3.deduped and not r4.deduped)
    # 去重记录不落进 initDict
    d = msmod._live_init_dict("ev_dedup")
    check("_oo_event_seen 不下发给客户端",
          "_oo_event_seen" not in d["__Properties__"])

    print("\n[C] 不经过 RPC 也能推进任务")
    st = fresh("ev_direct")
    activate(st, 50290)
    check("任务 50290 初始为进行中",
          story.story_state(st.props, 50290) == story.TASK_STATE_DOING)
    res = events.apply_event(st, events.ev_submit_item(1, 1))
    check("apply_event 命中任务 50290", 50290 in res.hits, res.hits)
    check("apply_event 只检测、不落状态（仍为进行中）",
          story.story_state(st.props, 50290) == story.TASK_STATE_DOING)

    print("\n[D] 4 种事件类型各能推进对应剧情任务")
    cases = [
        ("SubmitItem", 50290, lambda: events.ev_submit_item(1, 1)),
        ("AvatarAttr", 7007, lambda: events.ev_avatar_attr("level", 3)),
        ("GetItem(itemId)", 7017, lambda: events.ev_get_item(701, 1)),
        ("GetItem(itemType)", 7016, lambda: events.ev_get_item(1, 1, item_type=9)),
        ("CharData", 7020, lambda: events.ev_char_data(level=10)),
    ]
    for name, tid, mk in cases:
        stx = fresh("ev_t%d" % tid)
        tt = activate(stx, tid)
        check("任务 %d 已激活（TaskType=%s）" % (tid, tt), tt == story.TASK_STATE_DOING, tt)
        res = events.apply_event(stx, mk())
        check("%s 命中任务 %d" % (name, tid), tid in res.hits, res.hits)

    print("\n[E] 链式任务自动检查")
    st = fresh("ev_chain")
    # 201000 的 Parameter1 = [1031]，1031 完成即本任务完成
    activate(st, 201000)
    check("链式任务 201000 已激活",
          story.story_state(st.props, 201000) == story.TASK_STATE_DOING)
    res0 = events.tick(st)
    check("前置未完成时不命中", 201000 not in res0.chain_hits, res0.chain_hits)
    story.finish_story_task(st, 1031, 0)
    check("前置 1031 已手工完成", finished(st, 1031))
    res1 = events.tick(st)
    check("前置完成后 tick 命中链式任务", 201000 in res1.chain_hits,
          res1.chain_hits)
    res2 = events.apply_event(st, events.ev_submit_item(1, 1))
    check("任何事件之后也会检查链式任务", 201000 in res2.chain_hits,
          res2.chain_hits)

    print("\n[F] 端到端：3 个 RPC 真的能推进剧情")
    # F1 SubmitItem_CS
    st = fresh("ev_rpc_sub")
    activate(st, 50290)
    frames = guarded_handle(erpc.RPC_INDEX["SubmitItem_CS"],
                         msmod.mp_arr([101, 1]), 1, uid="ev_rpc_sub")
    check("SubmitItem_CS 完成任务 50290", finished(st, 50290))
    check("SubmitItem_CS 返回同步帧", len(frames) >= 2, len(frames))

    # F2 RecordItemObtain_CS
    st = fresh("ev_rpc_get")
    activate(st, 7017)          # 事件 {itemId: 701}
    frames = guarded_handle(erpc.RPC_INDEX["RecordItemObtain_CS"],
                         msmod.mp_arr([{"itemId": 701, "count": 1}]),
                         1, uid="ev_rpc_get")
    check("RecordItemObtain_CS 完成任务 7017", finished(st, 7017))
    check("RecordItemObtain_CS 返回同步帧", len(frames) >= 2, len(frames))

    # F3 UpgradeCharacter_CS
    st = fresh("ev_rpc_char")
    activate(st, 7020)          # 事件 {level: 10}
    bag = st.props.get("characterBag") or {}
    key = next((k for k in bag if isinstance(k, int)), None)
    check("存档里有角色", key is not None, list(bag.keys())[:5])
    bag[key]["CharLevel"] = 9
    st.set("characterBag", bag)
    # ★ 升级语义修复后：sendItems 必须是真材料（201 CharExp=200）。
    #   9->10 级需要 CharUpgradeData[9001][8].costExp=290 → 给 2 个（400exp，
    #   扣 290 剩 110 溢出进 CharExp），与客户端 UI 的"恰好升一级"一致。
    st.props.setdefault("item_bag", {})["901"] = {
        "__id__": 990011, "id": "901", "itemId": 201, "num": 2}
    st.set("item_bag", st.props["item_bag"])
    frames = guarded_handle(erpc.RPC_INDEX["UpgradeCharacter_CS"],
                         msmod.mp_arr([key, {"201": 2}]), 1, uid="ev_rpc_char")
    check("角色升到 10 级",
          int((st.props["characterBag"][key] or {}).get("CharLevel") or 0) == 10,
          (st.props["characterBag"][key] or {}).get("CharLevel"))
    check("UpgradeCharacter_CS 完成任务 7020", finished(st, 7020))

    # F4 客户端真实签名：SubmitItem_CS(submit_id, itemDict)
    st = fresh("ev_rpc_subid")
    activate(st, 50290)
    row = gamedata.table("ItemSubmitData") or {}
    sid = next(iter(row)) if row else None
    check("ItemSubmitData 有数据", bool(row), len(row))
    need = (row.get(sid) or {}).get("itemId") if sid else None
    need_ids = []
    if isinstance(need, dict):
        for k in need.keys():
            if not str(k).startswith("__") and k != "KeyList":
                need_ids.append(int(k))
    frames = guarded_handle(erpc.RPC_INDEX["SubmitItem_CS"],
                         msmod.mp_arr([sid, {need_ids[0]: 1} if need_ids else {}]),
                         1, uid="ev_rpc_subid")
    check("submit_id 字符串参数不抛异常",
          isinstance(frames, list) and len(frames) >= 1, frames)
    check("SubmitItem_CS 用 submit_id 签名也能完成任务 50290",
          finished(st, 50290))

    print("\n[G] 服务端主动推进：登录 tick + pending chain")
    st = fresh("ev_login")
    activate(st, 201000)          # 链式任务，前置 1031
    story.finish_story_task(st, 1031, 0)
    st.save()
    pstate._STATES.pop("ev_login", None)      # 模拟进程重启
    msmod._live_init_dict("ev_login")         # 登录
    st2 = pstate.get_state(uid="ev_login")
    pend = st2.props.get("_oo_pending_chain")
    check("登录 tick 把命中的链式任务记进 _oo_pending_chain",
          isinstance(pend, list) and 201000 in pend, pend)
    # 第一条 RPC 应该把它补完
    frames = guarded_handle(erpc.RPC_INDEX["SetSignature_CS"],
                         msmod.mp_arr(["hi"]), 1, uid="ev_login")
    check("第一条 RPC 补完链式任务 201000", finished(st2, 201000))
    check("补完时推了同步帧", len(frames) >= 2, len(frames))
    check("_oo_pending_chain 已清空",
          "_oo_pending_chain" not in st2.props)

    print("\n[I] ★ 审计 P0 修复回归")
    # P0-1：登录 tick 的 hits 不能丢（之前只记 chain_hits，AvatarAttr 命中的任务永远完不成）
    st = fresh("ev_p01")
    activate(st, 400015)          # 需要 AvatarAttr level=3
    st.props["level"] = 3
    st.save()
    pstate._STATES.pop("ev_p01", None)
    msmod._live_init_dict("ev_p01")
    st = pstate.get_state(uid="ev_p01")
    pend = st.props.get("_oo_pending_chain") or []
    check("P0-1 登录 tick 把 hits 记进 pending", 400015 in pend, pend)
    guarded_handle(erpc.RPC_INDEX["SetSignature_CS"], msmod.mp_arr(["x"]),
                1, uid="ev_p01")
    check("P0-1 首条 RPC 完成任务 400015", finished(st, 400015))

    # P0-4：7006/7007 共用 level=3，AvatarAttr 不能按 level 去重
    st = fresh("ev_p04")
    activate(st, 7006)
    activate(st, 7007)
    r1 = events.apply_event(st, events.ev_avatar_attr("level", 3))
    check("P0-4 第一次 level=3 同时命中 7006 和 7007",
          7006 in r1.hits and 7007 in r1.hits, r1.hits)
    erpc._finish_story_tasks(st, [7006])
    r2 = events.apply_event(st, events.ev_avatar_attr("level", 3))
    check("P0-4 第二次 level=3 仍能命中 7007（没被去重挡住）",
          7007 in r2.hits and not r2.deduped, (r2.hits, r2.deduped))

    # P0-2：服务端自己发 GetItem（不能只靠客户端 RecordItemObtain_CS）
    st = fresh("ev_p02")
    activate(st, 7017)            # 需要 GetItem itemId=701
    hits = erpc._emit_get_item(st, 701, 1)
    check("P0-2 _emit_get_item 返回命中的任务", 7017 in (hits or []), hits)

    # P1-4：递归里抛异常也要落盘（save 在 finally）
    st = fresh("ev_p14")
    activate(st, 50290)
    _saved = []
    _orig_save = st.save
    st.save = lambda *a, **k: (_saved.append(1), _orig_save(*a, **k))[1]
    try:
        erpc._finish_story_tasks(st, [50290])
    finally:
        st.save = _orig_save
    check("P1-4 _finish_story_tasks 结束时会 save", bool(_saved), len(_saved))

    print("\n[H] 帧安全：以上路径的帧喂进影子缓存不崩")
    st = fresh("ev_shadow")
    activate(st, 50290)
    # ★ pre 必须在 activate 之后取：activate() 是测试辅助、不发帧，
    #   而真实游戏里任务是通过 PostTasks 用 OP_ADD 推给客户端的。
    pre = msmod._live_init_dict("ev_shadow")
    frames = []
    frames += guarded_handle(erpc.RPC_INDEX["SubmitItem_CS"],
                          msmod.mp_arr([101, 1]), 1, uid="ev_shadow")
    frames += guarded_handle(erpc.RPC_INDEX["RecordItemObtain_CS"],
                          msmod.mp_arr([{"itemId": 701}]), 2, uid="ev_shadow")
    sc = ShadowCache()
    sc.init_property(pre["__Properties__"])
    for f in frames:
        sc.apply_frame(f)
    check("事件总线产出的帧在客户端不会崩", not sc.errors, sc.errors[:2])
    check("确实处理了属性同步帧", sc.frame_count > 0, sc.frame_count)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
