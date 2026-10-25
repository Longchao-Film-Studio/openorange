#!/usr/bin/env python
"""副本链路测试（入口 -> 结算 -> 剧情推进 -> 下次登录跳过序章）。

逆向依据：
  entities/clientavatar.lua:178-203 CheckEnterPrologue()
    if not self.story_finished[1] then
      Storyline_StartGame_CS(1004, {modeId=1, storyId=1})
        -> Cmd_Avatar_StartGame_CS {match_info}
        -> successCb(resp.spaceInfo) -> SpaceUtils.enterOnlineHub
    else LoginSuccess end

  entities/avatarcomponent/avatarstorylinecomp.lua:46-75
    resp.spaceInfo 要再套一层 .spaceInfo（enterOnlineHub 读 data.spaceInfo.copyId）

  entities/avatarcomponent/avatardungeoncomp.lua:217-222
    FinishDungeonSpace(finishInfo) -> Cmd_OfflineSpace_Finish_CS

  BDD 链条：
    StoryTask 1000 (TaskType=3, Parameter1=[1])
      -> TaskData[1] {TriggerEventList:[4001]}
      -> TaskEventData[4001] {FinishDungeon, {typeId:1004, modeId:1, level:1}}
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_dungeon_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config, gamedata, story
from game_server import entity_rpc as erpc
from game_server import ms as msmod
from game_server import player_state as pstate
from tests.frame_guard import guarded_handle  # noqa: E402

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

PROLOGUE_MATCH = {"match_type": 3, "system_type": 1004, "modeId": 1, "storyID": 1}

def run():
    print("\n[1] 序章配置链条（BDD）")
    gp = gamedata.gameplay(1004, 1) or {}
    check("GameplayData typeId=1004 modeId=1 是序章",
          gp.get("modeName") == "序章", gp.get("modeName"))
    line = gamedata.story_line(1) or {}
    check("StoryLineData[1].dungeonId = guide_01",
          line.get("dungeonId") == "guide_01", line.get("dungeonId"))
    copy = gamedata.level_copy("guide_01") or {}
    check("Level_CopyData[guide_01].copyName = 序章",
          copy.get("copyName") == "序章", copy.get("copyName"))
    check("rootLevel = [50001,50002,50003]",
          copy.get("rootLevel") == [50001, 50002, 50003], copy.get("rootLevel"))
    check("modeRule = main_plot", copy.get("modeRule") == "main_plot",
          copy.get("modeRule"))
    check("isOnline = false（离线副本）", copy.get("isOnline") is False,
          copy.get("isOnline"))

    print("\n[2] 新号 story_finished 为空 -> 客户端会走序章")
    st = fresh("dg_a")
    sf = st.props.get("story_finished") or {}
    check("story_finished 没有整数键 1", 1 not in sf, list(sf.keys())[:5])

    print("\n[3] Cmd_Avatar_StartGame_CS 返回正确的 spaceInfo")
    frames = guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                         msmod.mp_arr([PROLOGUE_MATCH]), 1, uid="dg_a")
    check("有应答帧", len(frames) >= 1, len(frames))
    # 解码应答，检查嵌套结构
    # ★ 2026-10-21 审计修正：以前这里用 msmod.parse_entity_frame() 解 S2C 帧，
    #   但那个函数是给 C2S 帧用的（返回 3-tuple），解不出来就 except 掉，
    #   然后伪造一个 resp 再断言 —— 等于自己测自己。现在改成正确解码：
    #   去掉 6 字节帧头 -> pb_parse -> field4 是 msgpack 参数。
    resp = None
    for f in frames:
        if not isinstance(f, (bytes, bytearray)):
            continue
        fields = msmod.pb_parse(bytes(f[6:]))
        if fields.get(2, [b""])[0] != b"__rpc_response__":
            continue
        params = fields.get(4, [b""])[0]
        if not params:
            continue
        decoded, _used = msmod.mp_decode(params)
        if isinstance(decoded, (list, tuple)) and len(decoded) == 2:
            resp = decoded[1]
        break
    check("能解出真实应答（不是伪造的）", isinstance(resp, dict), type(resp))
    sp = (resp or {}).get("spaceInfo") or {}
    inner = sp.get("spaceInfo") or {}
    check("resp.spaceInfo 存在（嵌套第一层）", bool(sp), sp)
    check("resp.spaceInfo.spaceInfo 存在（嵌套第二层）", bool(inner), inner)
    check("copyId = guide_01", inner.get("copyId") == "guide_01",
          inner.get("copyId"))
    check("levelIndex = 1（getCopy 是 1-based）", inner.get("levelIndex") == 1,
          inner.get("levelIndex"))

    print("\n[4] 结算 -> 剧情推进 + story_finished[1] 置真")
    st2 = fresh("dg_b")
    # ★ P1 阶段 N（审计 P0-4）：结算必须对应一次 StartGame。
    #   以前这里不 StartGame 直接发结算，那是**伪造路径**（审计实测能凭空完成 1000）。
    #   现在服务端只信 _oo_cur_match，所以必须先 StartGame。
    guarded_handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                msmod.mp_arr([{"system_type": 1004, "storyID": 1}]),
                1, uid="dg_b")
    check("StartGame 后 _oo_cur_copy = guide_01",
          st2.props.get("_oo_cur_copy") == "guide_01",
          st2.props.get("_oo_cur_copy"))
    guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 1004, "modeId": 1, "level": 1}]),
                2, uid="dg_b")
    check("StoryTask 1000 = Finished(6)",
          story.story_state(st2.props, 1000) == story.TASK_STATE_FINISHED,
          story.story_state(st2.props, 1000))
    check("StoryTask 1003 自动变 Doing(3)",
          story.story_state(st2.props, 1003) == story.TASK_STATE_DOING,
          story.story_state(st2.props, 1003))
    check("story_finished[1] = True",
          (st2.props.get("story_finished") or {}).get(1) is True,
          (st2.props.get("story_finished") or {}).get(1))

    print("\n[5] 下次登录：story_finished[1] 已真 -> 客户端跳过序章进街区")
    pstate._STATES.clear()
    d = msmod._live_init_dict("dg_b")["__Properties__"]
    check("initDict 里 story_finished 有整数键 1",
          1 in (d.get("story_finished") or {}),
          list((d.get("story_finished") or {}).keys())[:5])

    print("\n[6] 没 StartGame 直接结算 -> 不推进剧情（P0-4 防伪造）")
    st3 = fresh("dg_c")
    guarded_handle(erpc.RPC_INDEX["Cmd_OfflineSpace_Finish_CS"],
                msmod.mp_arr([{"typeId": 9999, "modeId": 1, "level": 1}]),
                3, uid="dg_c")
    check("★ 没 StartGame 时客户端伪造的 typeId 被忽略（1000 仍 Doing）",
          story.story_state(st3.props, 1000) == story.TASK_STATE_DOING,
          story.story_state(st3.props, 1000))
    check("1000 仍是 Doing",
          story.story_state(st3.props, 1000) == story.TASK_STATE_DOING,
          story.story_state(st3.props, 1000))
    check("story_finished[1] 仍为空",
          (st3.props.get("story_finished") or {}).get(1) is not True)

    print("\n[7] Storyline_FinishGame 也能置 story_finished")
    st4 = fresh("dg_d")
    guarded_handle(erpc.RPC_INDEX["Storyline_FinishGame"],
                msmod.mp_arr([1, True, 120]), 4, uid="dg_d")
    check("story_finished[1] = True",
          (st4.props.get("story_finished") or {}).get(1) is True,
          (st4.props.get("story_finished") or {}).get(1))
    # （原来这里有个 check(..., True) 恒真断言，删掉 —— 下面的 st5 才是真检验）
    st5 = fresh("dg_e")
    guarded_handle(erpc.RPC_INDEX["Storyline_FinishGame"],
                msmod.mp_arr([1, False, 5]), 5, uid="dg_e")
    check("passed=False 时不变",
          (st5.props.get("story_finished") or {}).get(1) is not True,
          (st5.props.get("story_finished") or {}).get(1))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())
