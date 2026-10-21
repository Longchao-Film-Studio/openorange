#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""副本结算（P0）+ 战斗教学/道具记录（P0.5）测试。

逆向依据：
  serverbattle.lua:721-758      onGameFinish 构造 finishInfo
  logicplayerstaticscomp.lua:128-174  setEndResult（battle_items = 净获得）
  avatarsettlementcomp.lua:231  AvatarSpace_SpaceFinish_SC 是客户端结算入口
  avatarsettlementcomp.lua:154-178  GoBackToWhere 用 toStreetId/toStreetPosId
  config.lua:9843               AvatarSpace_SpaceFinish_SC = {"table"}
  config.lua:10084              AvatarSystemGuideComp.battle_teach_state
  battle_main_panel.lua:513-516  newVal == 0 才弹教学

注意：本测试用独立临时存档目录，不碰 work/player_state 里的真实账号。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_settle_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (ms as msmod, entity_rpc as erpc, player_state as pstate,
                         config, story)

passed, failed = [], []

SPACE_FINISH_SC = 1634212414


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


def bag_items(st):
    bag = st.props.get("item_bag") or {}
    out = {}
    for k, v in bag.items():
        if isinstance(v, dict) and isinstance(v.get("itemId"), int):
            out[v["itemId"]] = v.get("num")
    return out


def find_s2c(frames, method):
    """在返回帧里找 server->client 的 entityMessage，返回 (index, params_bytes)。"""
    for f in frames:
        if not isinstance(f, (bytes, bytearray)):
            continue
        if method.encode() not in f:
            continue
        body = bytes(f[6:])          # 去掉 [u32 len][u16 type]
        fields = msmod.pb_parse(body)
        idx = fields.get(3, [0])[0]
        params = fields.get(4, [b""])[0]
        return idx, params
    return None, None


def start_game(uid):
    return erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                       msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                      "modeId": 1, "storyID": 1}]),
                       1, uid=uid)


FINISH_INFO = {
    "toStreetId": 101,
    "toStreetPosId": 50005,
    "finishReason": 5,
    "gameState": 30,
    "userList": {"settle_a": 1},
    "settle_a": {"win": True, "useTime": 95, "collectCount": 0,
                 "battle_items": {203: 3, 103: 500}},
}


def run():
    print("\n[1] StartGame 要存下 match_info（结算时反查 typeId）")
    st = fresh("settle_a")
    start_game("settle_a")
    m = st.props.get("_oo_cur_match") or {}
    check("_oo_cur_match 已保存", bool(m), m)
    check("system_type = 1004", m.get("system_type") == 1004, m.get("system_type"))
    check("modeId = 1", m.get("modeId") == 1, m.get("modeId"))
    check("_oo_cur_copy = guide_01",
          st.props.get("_oo_cur_copy") == "guide_01",
          st.props.get("_oo_cur_copy"))

    print("\n[2] 结算发奖（battle_items -> item_bag）")
    before = bag_items(st)
    frames = erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                         msmod.mp_arr([FINISH_INFO]), 26, uid="settle_a")
    after = bag_items(st)
    check("道具 203 从无到 3",
          before.get(203) is None and after.get(203) == 3,
          (before.get(203), after.get(203)))
    check("道具 103 增加 500",
          after.get(103, 0) - before.get(103, 0) == 500,
          (before.get(103), after.get(103)))

    print("\n[3] 必须下发 AvatarSpace_SpaceFinish_SC")
    idx, params = find_s2c(frames, "AvatarSpace_SpaceFinish_SC")
    check("帧里有 AvatarSpace_SpaceFinish_SC", idx is not None)
    check("index = 1634212414", idx == SPACE_FINISH_SC, idx)
    settle = None
    if params:
        decoded, _used = msmod.mp_decode(params)
        if isinstance(decoded, list) and decoded:
            settle = decoded[0]
    check("参数可解码出结算表", isinstance(settle, dict), type(settle))
    if isinstance(settle, dict):
        check("回传 toStreetId = 101", settle.get("toStreetId") == 101,
              settle.get("toStreetId"))
        check("回传 toStreetPosId = 50005", settle.get("toStreetPosId") == 50005,
              settle.get("toStreetPosId"))
        mine = settle.get("settle_a") or {}
        check("有本人结算数据", bool(mine), mine)
        check("win = True", mine.get("win") is True, mine.get("win"))
        check("useTime = 95", mine.get("useTime") == 95, mine.get("useTime"))
        rl = mine.get("reward_item_list") or []
        check("reward_item_list 有 2 项", len(rl) == 2, rl)
        ids = sorted(x.get("item_id") for x in rl if isinstance(x, dict))
        check("奖励是 103 和 203", ids == [103, 203], ids)
        check("有 userList", isinstance(settle.get("userList"), dict))
        mi = settle.get("match_info") or {}
        check("match_info.system_type = 1004", mi.get("system_type") == 1004, mi)

    print("\n[4] 结算同时推进剧情")
    check("StoryTask 1000 = Finished",
          story.story_state(st.props, 1000) == story.TASK_STATE_FINISHED,
          story.story_state(st.props, 1000))
    check("StoryTask 1003 = Doing",
          story.story_state(st.props, 1003) == story.TASK_STATE_DOING,
          story.story_state(st.props, 1003))
    check("story_finished[1] = True",
          (st.props.get("story_finished") or {}).get(1) is True)

    print("\n[5] 结算后清掉副本上下文")
    check("_oo_cur_copy 已清", "_oo_cur_copy" not in st.props)
    check("_oo_cur_match 已清", "_oo_cur_match" not in st.props)

    print("\n[6] 没有 battle_items 时也要下发结算（不能卡住）")
    st2 = fresh("settle_b")
    start_game("settle_b")
    info2 = dict(FINISH_INFO)
    info2["settle_b"] = {"win": True, "useTime": 10}
    frames2 = erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_FinishGame_CS"],
                          msmod.mp_arr([info2]), 27, uid="settle_b")
    idx2, _p2 = find_s2c(frames2, "AvatarSpace_SpaceFinish_SC")
    check("仍下发 SpaceFinish_SC", idx2 == SPACE_FINISH_SC, idx2)

    print("\n[7] P0.5：BattleTeach_Activate_CS")
    st3 = fresh("settle_c")
    bts = st3.props.get("battle_teach_state") or {}
    check("battle_teach_state 在 initDict 里", isinstance(bts, dict), type(bts))
    frames3 = erpc.handle(erpc.RPC_INDEX["BattleTeach_Activate_CS"],
                          msmod.mp_arr([101]), 28, uid="settle_c")
    check("battle_teach_state[101] = 0",
          (st3.props.get("battle_teach_state") or {}).get(101) == 0,
          st3.props.get("battle_teach_state"))
    check("有同步帧（OP_ADD）", len(frames3) >= 2, len(frames3))
    # 重复激活不应重复推
    frames3b = erpc.handle(erpc.RPC_INDEX["BattleTeach_Activate_CS"],
                           msmod.mp_arr([101]), 29, uid="settle_c")
    check("重复激活只有 ACK", len(frames3b) == 1, len(frames3b))

    print("\n[8] P0.5：RecordItemObtain_CS 只回 ACK")
    frames4 = erpc.handle(erpc.RPC_INDEX["RecordItemObtain_CS"],
                          msmod.mp_arr([203]), 30, uid="settle_c")
    check("返回 1 帧", len(frames4) == 1, len(frames4))
    check("是应答帧", b"__rpc_response__" in frames4[0])

    print("\n[9] --dungeon-last-only：只下发最后一关")
    # 序章 rootLevel = [50001, 50002, 50003]（50003 是千手Boss战）
    # 客户端 dispatchhubonline.lua:49-59 优先用服务端给的 spaceInfo.levelIds
    def start_levels(flag, uid):
        config.configure_dungeon_last_only(flag)
        fresh(uid)
        fs = erpc.handle(erpc.RPC_INDEX["Cmd_Avatar_StartGame_CS"],
                         msmod.mp_arr([{"match_type": 3, "system_type": 1004,
                                       "modeId": 1, "storyID": 1}]),
                         1, uid=uid)
        for f in fs:
            if not isinstance(f, (bytes, bytearray)):
                continue
            body = bytes(f[6:])
            fields = msmod.pb_parse(body)
            params = fields.get(4, [b""])[0]
            if not params:
                continue
            try:
                decoded, _u = msmod.mp_decode(params)
            except Exception:
                continue
            if not (isinstance(decoded, list) and len(decoded) == 2):
                continue
            sp = (decoded[1] or {}).get("spaceInfo") or {}
            inner = sp.get("spaceInfo") or {}
            if "levelIds" in inner:
                return inner["levelIds"]
        return None

    lv_off = start_levels(False, "dlo_off")
    check("默认下发三关 [50001,50002,50003]",
          lv_off == [50001, 50002, 50003], lv_off)
    lv_on = start_levels(True, "dlo_on")
    check("--dungeon-last-only 只下发 [50003]（千手Boss战）",
          lv_on == [50003], lv_on)
    config.configure_dungeon_last_only(False)   # 复位，别影响别的测试

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
