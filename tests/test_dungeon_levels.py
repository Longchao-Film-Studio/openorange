#!/usr/bin/env python
"""序章三关推进测试（Cmd_Avatar_EnterNext_CS / levelInfo / 关卡上下文）。

为什么单独一个文件：
    序章 guide_01 有 **3 个 rootLevel**（50001 / 50002 / 50003），也就是「打三关」。
    每关之间客户端发 Cmd_Avatar_EnterNext_CS {copyIndex}，服务端必须回 levelInfo，
    否则 HubPlayer.lua:139 直接 nil 索引异常（实测过：下一关的 ServerBattle 建不起来，
    角色在空场景里一直坠落）。而 Cmd_Avatar_EnterNext_CS 之前**一条测试都没有** ——
    它是唯一没被覆盖的副本 RPC。

同时守住另一件事：结算时喂给剧情引擎的 levelId 必须是**最后一关**，
否则多关副本的 FinishDungeon 事件会按第一关匹配，剧情推错。

用法: python tests/test_dungeon_levels.py
"""
import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_dglv_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, gamedata, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from game_server.story import events as _events
from tests.frame_guard import guarded_handle  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_cache import frames_safe  # noqa: E402

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
    msmod.live_init_dict(uid)
    return pstate.get_state(uid=uid)

PROLOGUE_MATCH = {"match_type": 3, "system_type": 1004, "modeId": 1, "storyID": 1}

def parse_resp(frames):
    """从帧列表里解出 RPC 应答的 response（field2=__rpc_response__ 的那条）。"""
    for f in frames:
        if not isinstance(f, (bytes, bytearray)):
            continue
        try:
            fields = msmod.pb_parse(bytes(f[6:]))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
        if fields.get(2, [b""])[0] != b"__rpc_response__":
            continue
        params = fields.get(4, [b""])[0]
        if not params:
            continue
        decoded, _used = msmod.mp_decode(params)
        if isinstance(decoded, (list, tuple)) and len(decoded) == 2:
            return decoded[1]
    return None

def run():
    print("\n[1] 序章配置：guide_01 就是三关")
    cfg = gamedata.level_copy("guide_01") or {}
    roots = cfg.get("rootLevel") or []
    check("guide_01 有 3 个 rootLevel（打三关）", len(roots) == 3, roots)
    check("三关依次是 50001/50002/50003", roots == [50001, 50002, 50003], roots)
    check("Cmd_Avatar_EnterNext_CS 在索引表里",
          "Cmd_Avatar_EnterNext_CS" in erpc.RPC_INDEX)
    check("Cmd_Avatar_EnterNext_CS 有处理器",
          erpc.RPC_INDEX.get("Cmd_Avatar_EnterNext_CS") in erpc.HANDLERS)

    print("\n[2] StartGame 一次下发三关")
    fresh("dglv_a")
    _pre = msmod.live_init_dict("dglv_a")
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                         msmod.mp_arr([PROLOGUE_MATCH]), 1, uid="dglv_a")
    resp = parse_resp(frames)
    inner = ((resp or {}).get("spaceInfo") or {}).get("spaceInfo") or {}
    check("spaceInfo.spaceInfo.levelIds = 三关", inner.get("levelIds") == roots,
          inner.get("levelIds"))
    st = pstate.get_state(uid="dglv_a")
    check("进本时记下当前关卡 = 第一关 50001",
          st.props.get("_oo_cur_level") == 50001, st.props.get("_oo_cur_level"))
    _sc, _errs = frames_safe(_pre, frames, "进副本")
    check("进副本的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n[3] 打完第一关 -> EnterNext(copyIndex=2) 回第二关 levelInfo")
    _pre = msmod.live_init_dict("dglv_a")
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_EnterNext_CS"],
                         msmod.mp_arr([2]), 2, uid="dglv_a")
    resp = parse_resp(frames)
    li = (resp or {}).get("levelInfo") or {}
    check("应答带 levelInfo（缺了客户端 HubPlayer.lua:139 必崩）", bool(li), resp)
    check("levelInfo.levelProgress 存在", isinstance(li.get("levelProgress"), dict), li)
    check("levelInfo.randSeed 是整数", isinstance(li.get("randSeed"), int), li)
    check("levelInfo.runTime 是整数", isinstance(li.get("runTime"), int), li)
    check("当前关卡更新为 50002", st.props.get("_oo_cur_level") == 50002,
          st.props.get("_oo_cur_level"))
    _sc, _errs = frames_safe(_pre, frames, "切第二关")
    check("切关的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n[4] 打完第二关 -> EnterNext(copyIndex=3) 回第三关")
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_EnterNext_CS"],
                         msmod.mp_arr([3]), 3, uid="dglv_a")
    resp = parse_resp(frames)
    check("第三关也带 levelInfo", bool((resp or {}).get("levelInfo")), resp)
    check("当前关卡更新为 50003", st.props.get("_oo_cur_level") == 50003,
          st.props.get("_oo_cur_level"))

    print("\n[5] 越界 copyIndex 不崩、也不改上下文")
    for bad in (0, 99, -1):
        frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_EnterNext_CS"],
                             msmod.mp_arr([bad]), 4, uid="dglv_a")
        check(f'copyIndex={bad} 不抛异常', isinstance(frames, list), frames)
        check(f'copyIndex={bad} 不改当前关卡',
              st.props.get("_oo_cur_level") == 50003, st.props.get("_oo_cur_level"))
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_EnterNext_CS"],
                         msmod.mp_arr(["x"]), 5, uid="dglv_a")
    check("非整数 copyIndex 不抛异常", isinstance(frames, list), frames)

    print("\n[6] 三关打完结算 -> 剧情推进，且 levelId 用最后一关")
    seen = []
    _orig = _events.apply_event

    def _spy(st_, event, now=None):
        with contextlib.suppress(Exception):
            seen.append((event.type, dict(event.params or {})))
        return _orig(st_, event, now)

    _events.apply_event = _spy
    try:
        _pre = msmod.live_init_dict("dglv_a")
        frames = guarded_handle(
            erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
            msmod.mp_arr([{"toStreetId": 101, "toStreetPosId": 0,
                           "finishReason": 0,
                           "dglv_a": {"win": True, "useTime": 300,
                                      "collectCount": 0,
                                      "battle_items": {"101": 3}}}]),
            6, uid="dglv_a")
    finally:
        _events.apply_event = _orig

    fin = [p for (t, p) in seen if t == _events.EV_FINISH_DUNGEON]
    check("结算发了 FinishDungeon 事件", bool(fin), seen)
    check("事件参数 typeId=1004 / modeId=1",
          fin and fin[0].get("typeId") == 1004 and fin[0].get("modeId") == 1,
          fin[:1])
    check("事件参数 levelId = 最后一关 50003",
          fin and fin[0].get("levelId") == 50003, fin[:1])
    check("StoryTask 1000 = Finished(6)",
          story.story_state(st.props, 1000) == story.TASK_STATE_FINISHED,
          story.story_state(st.props, 1000))
    check("StoryTask 1003 自动变 Doing(3)",
          story.story_state(st.props, 1003) == story.TASK_STATE_DOING,
          story.story_state(st.props, 1003))
    check("story_finished[1] = True",
          (st.props.get("story_finished") or {}).get(1) is True,
          (st.props.get("story_finished") or {}).get(1))
    check("结算后清掉关卡上下文", "_oo_cur_level" not in st.props,
          st.props.get("_oo_cur_level"))
    check("结算后回到街区 101", st.props.get("street_id") == 101,
          st.props.get("street_id"))
    _sc, _errs = frames_safe(_pre, frames, "三关结算")
    check("结算的帧在客户端不会崩", not _errs, _errs[:2])

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
