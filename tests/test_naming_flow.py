#!/usr/bin/env python
"""取名流程断链 P0 整改的守护测试（2026-10-22）。

★ 背景：新号进序章不弹取名面板。根因是 initDict 把「已取名」的状态预填了。
  详见 docs/整改清单-取名流程断链.md。

★ 本测试锁住的东西（每项都对应一个 P0）
  [1] P0-1/P0-2  initDict 必须是「未取名」状态（官方默认 "" / 0）
  [2] P0-1/P0-2  按客户端 NamingCheckTemp 的判据模拟，必须判 Undone
  [3] P0-7       portrait_bag / player_card_bag 有 {1,2}，其余 5 个 bag 保持空
  [4] P0-7b      取名后按性别写 player_card_id（男2/女1）
  [5]          取名后判据变 Done（下不再弹面板）
  [6] P0-3      scaffold(--unlock-all) 必须满足 checkIsCanEnterStreet 四条件
  [7]          新号不该满足 checkIsCanEnterStreet（它还没取名/没过序章）

用法: python tests/test_naming_flow.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_naming_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import (
    gamedata,  # noqa: E402
    rpc_index,  # noqa: E402
    scaffold,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []
_TAG = ""

_CN = "\u672a\u5177\u540d\u5bfc\u6f14"   # "未具名导演"


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return "{}_{}".format(name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u), u


def _is_nil_or_empty(v):
    return v is None or v == ""


def naming_needs_input(st):
    """照抄客户端 NamingCheckTemp:onStartFlowIn() 的判据
    （battle/public/system/nodeskill/viewnode/viewuinode.lua:1402-1416）。

    返回 True 表示「需要取名」（客户端走 Undone 分支、会弹 CreatingRolePanel）。
    """
    name = st.props.get("player_name")
    gender = st.props.get("gender")
    return _is_nil_or_empty(name) or gender == 0


def can_enter_street(st):
    """照抄客户端 StreetStage:checkIsCanEnterStreet()
    （stage/streetstage.lua:167-185）的四个条件。"""
    sf = st.props.get("story_finished") or {}
    c1 = any(not str(k).startswith("__") for k in sf)
    c2 = not _is_nil_or_empty(st.props.get("player_name"))
    g = st.props.get("gender")
    c3 = g is not None and g != 0
    cb = st.props.get("characterBag") or {}
    c4 = any(not str(k).startswith("__") for k in cb)
    return c1 and c2 and c3 and c4, (c1, c2, c3, c4)


def _bag_keys(st, prop):
    d = st.props.get(prop) or {}
    return [k for k in d if not str(k).startswith("__")]


def run():
    global _TAG
    _TAG = "{:04x}".format(random.randint(0, 0xFFFF))
    del passed[:]
    del failed[:]

    print("\n[1] P0-1/P0-2：新号 initDict 必须是「未取名」状态")
    st1, _u1 = fresh("newbie")
    check("★ player_name 是空串（官方默认）",
          st1.props.get("player_name") == "", repr(st1.props.get("player_name")))
    check("★ player_name 不是显示占位符 {!r}".format(_CN),
          st1.props.get("player_name") != _CN, repr(st1.props.get("player_name")))
    check("★ gender == 0（官方默认，0=未选）",
          st1.props.get("gender") == 0, st1.props.get("gender"))
    check("story_finished 为空（新号走序章）",
          _bag_keys(st1, "story_finished") == [], _bag_keys(st1, "story_finished"))

    print("\n[2] 按客户端 NamingCheckTemp 判据模拟")
    check("★ 新号判定为「需要取名」（会弹 CreatingRolePanel）",
          naming_needs_input(st1) is True)

    print("\n[3] P0-7：装扮 bag 初始状态")
    pb = _bag_keys(st1, "portrait_bag")
    cb = _bag_keys(st1, "player_card_bag")
    check("★ portrait_bag 有 2 项（初始头像 id=1/2）", len(pb) == 2, pb)
    check("★ player_card_bag 有 2 项（男女主默认名片）", len(cb) == 2, cb)
    check("★ portrait_id = 1（Default_PlayerHead）",
          st1.props.get("portrait_id") == 1, st1.props.get("portrait_id"))
    check("player_card_id 初始为 0（等取名后按性别写）",
          st1.props.get("player_card_id") == 0, st1.props.get("player_card_id"))
    for prop in ("portrait_frame_bag", "player_card_frame_bag",
                 "player_title_bag", "chat_bubble_bag", "background_bag"):
        check("{} 保持空（有 type=0 默认项兜底）".format(prop),
              _bag_keys(st1, prop) == [], _bag_keys(st1, prop))

    print("\n[4] P0-7b：取名后按性别写 player_card_id")
    st4, u4 = fresh("male")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u963f\u5149", 1]), 1, uid=u4)
    check("★ 男(gender=1) -> player_card_id=2",
          st4.props.get("player_card_id") == 2, st4.props.get("player_card_id"))
    check("取名后 player_name 已写入",
          st4.props.get("player_name") == "\u963f\u5149",
          repr(st4.props.get("player_name")))
    check("取名后 gender 已写入", st4.props.get("gender") == 1,
          st4.props.get("gender"))

    st4b, u4b = fresh("female")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u963f\u5f71", 2]), 2, uid=u4b)
    check("★ 女(gender=2) -> player_card_id=1",
          st4b.props.get("player_card_id") == 1,
          st4b.props.get("player_card_id"))

    print("\n[5] 取名后判据变 Done（下不再弹面板）")
    check("★ 取名后 NamingCheckTemp 判 Done",
          naming_needs_input(st4) is False)

    print("\n[6] P0-3：scaffold(--unlock-all) 必须满足进街区四条件")
    st6, _u6 = fresh("scaffold")
    cfg = type("C", (), {"level": None, "unlock_all": False,
                         "all_tasks_done": True})()
    changed = scaffold.apply(st6.props, cfg)
    ok6, parts6 = can_enter_street(st6)
    check("★ scaffold 后能进街区（四条件全满足）", ok6, parts6)
    check("★ scaffold 补了 player_name",
          st6.props.get("player_name") == scaffold.SCAFFOLD_PLAYER_NAME,
          repr(st6.props.get("player_name")))
    check("★ scaffold 的 player_name 不是显示占位符 {!r}".format(_CN),
          st6.props.get("player_name") != _CN)
    check("★ scaffold 补了 gender=1", st6.props.get("gender") == 1,
          st6.props.get("gender"))
    check("scaffold 记录了改动", len(changed) > 0, len(changed))

    print("\n[7] 新号不该满足进街区四条件（还没取名/没过序章）")
    ok7, parts7 = can_enter_street(st1)
    check("★ 新号不能直接进街区", ok7 is False, parts7)


    print("\n[8] P1-2：取名按性别设主角（男9001/女9002 + 补 characterBag 9002）")
    st8, u8 = fresh("p12girl")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u963f\u5f71", 2]), 8, uid=u8)
    cb8 = st8.props.get("characterBag") or {}
    check("★ 女导 -> character_choosen_char=9002",
          st8.props.get("character_choosen_char") == 9002,
          st8.props.get("character_choosen_char"))
    check("★ characterBag 补了 9002（女主）", 9002 in cb8, sorted(k for k in cb8 if isinstance(k, int)))
    check("★ 9002 的 useSkinId=900201（女主默认皮肤）",
          cb8.get(9002, {}).get("useSkinId") == 900201, cb8.get(9002, {}).get("useSkinId"))
    check("★ 9002 的 __id__ 不与 9001 撞号",
          cb8.get(9002, {}).get("__id__") != cb8.get(9001, {}).get("__id__"))
    check("★ 9001 仍保留（没被顶掉）", 9001 in cb8)

    st8b, u8b = fresh("p12boy")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u963f\u5149", 1]), 9, uid=u8b)
    cb8b = st8b.props.get("characterBag") or {}
    check("★ 男导 -> 保持 9001",
          st8b.props.get("character_choosen_char") == 9001,
          st8b.props.get("character_choosen_char"))
    check("★ 男导不补 9002（bag 只有 9001）",
          9002 not in cb8b, sorted(k for k in cb8b if isinstance(k, int)))

    print("\n[9] P1-7：FriendService.fetchFriends 返回空好友列表")
    _params = msmod.mp_map({"Ret": [True, ""], "Friends": []})
    _dec = msmod.mp_decode(_params)[0]
    check("★ Ret = [True, \"\"]（客户端 ret[1]=status, ret[2]=errmsg）",
          _dec.get("Ret") == [True, ""], _dec.get("Ret"))
    check("★ Friends = []（空好友列表）", _dec.get("Friends") == [], _dec.get("Friends"))
    # 模拟客户端剥掉 Ret 后（msgateclientrpchandler.lua:47 response.Ret=nil）
    _resp = {k: v for k, v in _dec.items() if k != "Ret"}
    check("★ 剥掉 Ret 后客户端拿到 Friends", _resp.get("Friends") == [], _resp)


    print("\n[10] 街区初始化数据：登录后 street_info 必须有当前街道条目")
    # ★ 修复「结算后无缝切街区」崩溃（2026-10-23 实机日志实锤）
    #   客户端进街区 getStreetBgm（StreetSystemMgrBase.lua:111-114）读
    #   getStreetInfoByID(street_id) -> street_info[street_id]，nil 会崩
    st10, u10 = fresh("streetinfo")
    si10 = st10.props.get("street_info") or {}
    sid10 = st10.props.get("street_id")
    check("★ 登录后 street_info[street_id] 存在（结算切街区不崩）",
          sid10 in si10, (sid10, [k for k in si10 if not str(k).startswith("__")]))
    e10 = si10.get(sid10) or {}
    check("★ 条目有 cd_choose（getStreetBgm 读它）", "cd_choose" in e10, list(e10.keys()))
    for _k in ("exploration_info", "prestige_awarded", "prestige_tasks",
               "treasure_box_info", "inter_actor_info"):
        check("★ 嵌套字段 {} 带 __id__".format(_k),
              isinstance(e10.get(_k), dict) and "__id__" in e10[_k], e10.get(_k))
    # 模拟客户端 getStreetBgm：streetInfo 非 nil
    check("★ 模拟 getStreetBgm：streetInfo 非 nil", si10.get(sid10) is not None)


    print("\n[11] 战斗教学：不在 InstructionData 表的 id 拒绝激活（防客户端崩）")
    # ★ 2026-10-23 实机日志实锤：序章 ActivateBattleTeach 节点 instructionID=312，
    #   但 InstructionData 表没有 312。若写 battle_teach_state[312]=0 并同步，
    #   客户端 onChangedBattleTeachState -> InstructionData[312]=nil ->
    #   Battle_Main_Panel.lua:625 崩 -> 神秘力量。
    _itab = gamedata.table("InstructionData") or {}
    check("★ InstructionData 没有 312（这就是崩源）",
          _itab.get("312") is None, "312" in _itab)
    check("★ InstructionData 有 101（合法教学）",
          _itab.get("101") is not None)
    st11, u11 = fresh("teach")
    guarded_handle(rpc_index.RPC_INDEX["BattleTeach_Activate_CS"],
                   msmod.mp_arr([312]), 11, uid=u11)
    _bts = st11.props.get("battle_teach_state") or {}
    check("★ 312 被拒绝（battle_teach_state 无 312，不推同步）",
          312 not in _bts, [k for k in _bts if isinstance(k, int)])
    guarded_handle(rpc_index.RPC_INDEX["BattleTeach_Activate_CS"],
                   msmod.mp_arr([101]), 12, uid=u11)
    _bts = st11.props.get("battle_teach_state") or {}
    check("★ 101 正常激活（值=0 弹教学 UI）",
          _bts.get(101) == 0, _bts.get(101))


    print("\n[12] 取名必须下发 player_name / gender 属性同步帧（否则进街区弹神秘力量）")
    # ★ 2026-10-23 实机客户端日志实锤：
    #   客户端 creatingrolepanel.lua:92-115 取名成功后不在本地设 player_name/gender，
    #   只依赖服务端同步帧。以前 _set_name 只写 props 不发帧 -> 客户端这两个值一直是
    #   ""/0 -> streetstage.lua:167 checkIsCanEnterStreet 条件 2/3 失败 ->
    #   SysTextData[29]「未知的神秘力量干扰了加载进程」-> 回登录。
    #   重登后靠 initDict 才正常（表现为「重登能进街区」）。
    def _sync_names(frames):
        out = []
        for fr in frames:
            f = bytes(fr)
            if len(f) < 6:
                continue
            if int.from_bytes(f[4:6], "little") != msmod.PROPERTY_SYNC_TYPE:
                continue
            fields = msmod.pb_parse(f[6:])
            nm = fields.get(4, [b""])[0]
            if isinstance(nm, (bytes, bytearray)):
                nm = nm.decode("utf-8", "replace")
            out.append(nm)
        return out

    st12, u12 = fresh("namesync")
    _fr12 = guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                           msmod.mp_arr(["\u5973\u5bfc\u6f14\u6d4b\u8bd5", 2]), 13, uid=u12)
    _names = _sync_names(_fr12)
    check("★ 取名响应含 player_name 同步帧", "player_name" in _names, _names)
    check("★ 取名响应含 gender 同步帧", "gender" in _names, _names)
    check("★ 取名响应含 player_card_id 同步帧",
          "player_card_id" in _names, _names)
    check("★ 取名响应含 character_choosen_char 同步帧",
          "character_choosen_char" in _names, _names)


    print("\n[13] 编队（队伍切换头像）随性别：取名女->9002；老号登录校正")
    # ★ 2026-10-23 实机反馈：街区右下角队伍切换头像显示男主。
    #   客户端 StreetV2Panel:initFormation（streetv2panel.lua:1311-1341）
    #   用 character_list[choosen_index].value[i]（charId）设队员头像
    #   （CharData[charId].DefaultSkin -> CharAppearanceData.FacialDrawing）。
    #   老号取名早于编队修复，编队仍 [9001,0,0] -> 男主头像。
    def _formation_ids(st0):
        cl0 = st0.props.get("character_list") or {}
        return {int(k): v for team in cl0.values() if isinstance(team, dict)
                for k, v in (team.get("value") or {}).items()
                if not str(k).startswith("__") and isinstance(v, int)}

    # 1) 取名女 -> 编队主角 9002（覆盖原 [9001,0,0]）
    st13, u13 = fresh("fmgirlf")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u7f16\u961f\u5973\u4e3b", 2]), 15, uid=u13)
    _f13 = _formation_ids(st13)
    check("★ 取名女后队1主角是 9002（队伍切换显示女主头像）",
          _f13.get(1) == 9002, _f13)

    # 2) 老号校正：gender=2 但编队是 9001（旧数据），登录时应被改回 9002
    st13b, u13b = fresh("oldhero")
    _cl = st13b.props.get("character_list") or {}
    for _team in _cl.values():
        if isinstance(_team, dict):
            _lst = _team.get("value") or {}
            for _k in list(_lst.keys()):
                if not str(_k).startswith("__") and int(_lst[_k] or 0) == 9001:
                    _lst[_k] = 9001  # 保持 9001（模拟旧号）
    st13b.props["gender"] = 2
    st13b.props["character_choosen_char"] = 9002
    st13b.set("character_list", _cl)
    st13b.save()
    # 重新走登录流程（initdict 里的校正块）
    msmod._live_init_dict(u13b)
    _stB = pstate.get_state(uid=u13b)
    _f13b = _formation_ids(_stB)
    check("★ 老号登录后编队 9001->9002 校正",
          _f13b.get(1) == 9002, _f13b)

    # 3) 取名男保持不变 9001
    st13c, u13c = fresh("fmboyf")
    guarded_handle(rpc_index.RPC_INDEX["RpcCommitNameGender"],
                   msmod.mp_arr(["\u7f16\u961f\u7537\u4e3b", 1]), 16, uid=u13c)
    _f13c = _formation_ids(st13c)
    check("★ 取名男后队1仍是 9001",
          _f13c.get(1) == 9001, _f13c)

    print("\n" + "=" * 56)
    print('通过 {} 项，失败 {} 项'.format(len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())