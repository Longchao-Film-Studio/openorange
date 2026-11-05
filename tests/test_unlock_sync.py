#!/usr/bin/env python
"""H2 回归：系统解锁帧必须真的发到客户端（不能只改服务端状态）。

背景（2026-10-27 实机 bug）：
  服务端日志显示「[story] 解锁系统 [2,4,55]」，但客户端 Player.log 里
  没有任何 on_unlocked_system_changed —— 因为解锁帧在两条路径上被丢：
    1) story/tasks.py::drain_event_task_sync 在没有任务同步时直接 return []；
    2) story/rpc.py::_emit_get_item 只返回 hits，丢掉 EventResult.frames。
  症状：解锁后必须重启客户端才看到按钮。

本测试对应用户可见行为：完成剧情任务 1005 后，返回帧里必须带
system_p.unlocked_system 的同步（不再依赖下次登录快照）。

用法: python tests/test_unlock_sync.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_unlock_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import story as story_mod  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.story import events as events_mod  # noqa: E402
from game_server.story import tasks as tasks_mod  # noqa: E402
from game_server.story.rpc import _emit_get_item, finish_story_tasks  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def parse_sync(pkt):
    mtype = int.from_bytes(pkt[4:6], "little")
    fields = msmod.pb_parse(pkt[6:])
    out = {"type": mtype}
    if 2 in fields:
        out["propertyid"] = fields[2][0]
    if 3 in fields:
        out["optype"] = fields[3][0]
    if 4 in fields:
        out["name"] = fields[4][0].decode("utf-8", "replace")
    if 5 in fields:
        out["value"] = msmod.mp_decode(fields[5][0])[0]
    return out


def syncs_of(frames):
    out = []
    for f in frames:
        if not isinstance(f, (bytes, bytearray)) or len(f) < 6:
            continue
        if int.from_bytes(f[4:6], "little") != msmod.PROPERTY_SYNC_TYPE:
            continue
        try:
            out.append(parse_sync(f))
        except Exception:
            continue
    return out


def fresh(uid):
    initdict.live_init_dict(uid)
    return pstate.get_state(uid=uid)


def run():
    print("\n[1] drain_event_task_sync：没有任务同步时也不能丢解锁帧")
    st = fresh("unlock_drain")
    fake = [b"\x00\x01fake-frame"]
    res = events_mod.EventResult(event=None, task_sync=[], frames=fake)
    got = tasks_mod.drain_event_task_sync(st, res)
    check("frames 原样返回", got == fake, got)

    print("\n[2] 真实路径：完成剧情任务 1005 必须立刻下发解锁帧")
    cfg = gamedata.story_task(1005) or {}
    check("1005 有奖励（保证走 _emit_get_item 路径）",
          bool(cfg.get("AwardItemList")), list(cfg)[:8])
    st2 = fresh("unlock_1005")
    unlocked0 = (st2.props.get("system_p") or {}).get("unlocked_system") or {}
    check("初始没有解锁头衔/龙胤/成就",
          not any(unlocked0.get(i) is True for i in (2, 4, 55)),
          {i: unlocked0.get(i) for i in (2, 4, 55)})
    # 先让 1005 进包并处于 Doing（真实流程也是这样），否则 _ready_to_finish 会拒绝
    story_mod.start_story_task(st2, 1005, 1793000000)
    frames = finish_story_tasks(st2, [1005], source="internal")
    us = (st2.props.get("system_p") or {}).get("unlocked_system") or {}
    check("服务端已解锁 2/4/55",
          all(us.get(i) is True for i in (2, 4, 55)),
          {i: us.get(i) for i in (2, 4, 55)})
    syn = syncs_of(frames)
    us_id = us.get("__id__")
    unlock_frames = [d for d in syn
                     if d.get("propertyid") == us_id and d.get("name") in ("2", "4", "55")]
    check("返回帧里带 unlocked_system 的同步（客户端不用重启）",
          bool(unlock_frames), [(d.get("name"), d.get("propertyid")) for d in syn][:8])
    # ★ 2026-11-05：optype 必须是 OP_ADD(2)，不是 OP_CHANGE(1)。
    #
    #   这条以前断言的是 OP_CHANGE —— 那是把 bug 写成了预期行为：
    #   客户端 entryAdded 里声明的是 system_p.unlocked_system（精确路径，
    #   没有通配，config.lua:10358），而 OP_CHANGE 的查表 key 是
    #   system_p.unlocked_system.*（customtype.lua:296-312 给容器键补 "*"），
    #   精确查表查不到（puppetpropertycomponent.lua:91-110 无通配匹配）。
    #
    #   回退机制救不了它：查不到会往上一层再查（:150-155），回退到
    #   changed["system_p.unlocked_system"] 那行，但那里声明的类型是
    #   customDict，实际对象是 BooleanDictKeyInt -> 类型校验不过（:146-148）
    #   只打 error 不调用。于是 83 个系统入口解锁后按钮都不出现，
    #   必须重登（走全量快照，不过回调）才可见。
    #
    #   OP_ADD 的查表 key 就是容器自身路径 system_p.unlocked_system，
    #   精确命中 entryAdded，而且 __OpAdd__ 没有类型校验（:263-271）。
    #   守卫：game_server/property/callback_contract.py + Tx._check_reachable。
    check("解锁帧 optype = 2(OP_ADD)、value=True（命中 entryAdded，客户端才收到）",
          all(d.get("optype") == msmod.OP_ADD and d.get("value") is True
              for d in unlock_frames),
          unlock_frames[:3])

    print("\n[3] _emit_get_item(frames=...)：GetItem 事件顺带的帧要带回")
    st3 = fresh("unlock_getitem")
    # 直接把 1005 标成已完成，模拟「完成瞬间之后又发道具」：下次 apply_event 会补解锁
    done = st3.props.get("done_story_tasks")
    # done_story_tasks 的值必须是 dict（客户端/服务端都按对象读，state=6 表示已完成）
    done[1005] = {"__id__": 990001, "id": 1005, "state": 6}
    st3.set("done_story_tasks", done)
    frames3 = []
    _emit_get_item(st3, 701, 1, frames=frames3)
    syn3 = syncs_of(frames3)
    check("GetItem 路径带出解锁帧",
          any(d.get("name") in ("2", "4", "55") for d in syn3),
          [(d.get("name"), d.get("propertyid")) for d in syn3][:8])

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
