#!/usr/bin/env python
"""主角（导演）唯一性：新号无角色，取名时按性别创建唯一主角。

不变量（本测试守着）：

  1. avatar_init 里**没有**任何主角（不预置 9001/9002），choosen=0，编队全 0；
  2. 主角 id 来自数据表 CommonConstData（male_player_char/female_player_char），
     并与客户端 tabledata/commondata/commonconstdata.lua:202-203 一致；
  3. RpcCommitNameGender 男 -> 只有男，女 -> 只有女（互斥，不是「加一个」）；
  4. 旧存档的「双主角」在登录自愈后只剩一个；
  5. 编队第 1 队 = [主角, 0, 0]（3 个格子），choosen 指向主角；
  6. 幂等：再跑一次不产生帧、不改状态。

用法: python tests/test_protagonist.py
"""
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_prot_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata, protagonist  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.ms import initdict  # noqa: E402
from game_server.property import factory  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_JSON = os.path.join(ROOT, "game_server", "avatar_init.json")
CLIENT_CONST = r"F:\haowancheng\archive\game_data\LuaAll_Decompiled2\tabledata\commondata\commonconstdata.lua"

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def fresh(uid="t"):
    props = copy.deepcopy(initdict.conv_int_keys(initdict.AVATAR_INIT_DICT["__Properties__"]))
    return pstate.PlayerState(uid=uid, props=props)


def chars(props):
    return sorted(k for k in (props.get("characterBag") or {}) if not str(k).startswith("__"))


def team_slots(props):
    cl = props.get("character_list") or {}
    team = cl.get(1) if isinstance(cl.get(1), dict) else cl.get("1")
    if not isinstance(team, dict):
        return None
    v = team.get("value") or {}
    return [v.get(1), v.get(2), v.get(3)]


def run():
    print("\n[1] avatar_init：新号没有任何主角")
    raw = json.load(open(INIT_JSON, encoding="utf-8"))["__Properties__"]
    check("characterBag 里没有角色",
          [k for k in raw["characterBag"] if not str(k).startswith("__")] == [],
          list(raw["characterBag"]))
    check("character_choosen_char = 0", raw.get("character_choosen_char") == 0,
          raw.get("character_choosen_char"))
    team = raw["character_list"]["#1"]
    check("编队第 1 队全是 0",
          [team["value"].get("#1"), team["value"].get("#2"), team["value"].get("#3")] == [0, 0, 0],
          team["value"])

    print("\n[2] 主角 id 来自数据表（不写死默认值）")
    check("male_player_char 可读",
          protagonist.male_id() == int(gamedata.const("male_player_char")), protagonist.male_id())
    check("female_player_char 可读",
          protagonist.female_id() == int(gamedata.const("female_player_char")), protagonist.female_id())
    if os.path.exists(CLIENT_CONST):
        lua = open(CLIENT_CONST, encoding="utf-8", errors="replace").read()
        check("与客户端表一致（male）",
              f"male_player_char = {protagonist.male_id()}" in lua, protagonist.male_id())
        check("与客户端表一致（female）",
              f"female_player_char = {protagonist.female_id()}" in lua, protagonist.female_id())
    else:
        print("  (客户端 Lua 表不存在，跳过交叉核对)")

    print("\n[3] 未选性别：保持「没有角色」")
    st = fresh("p0")
    protagonist.ensure(st, 0)
    check("仍然没有角色", chars(st.props) == [], chars(st.props))
    check("choosen = 0", int(st.props.get("character_choosen_char") or 0) == 0,
          st.props.get("character_choosen_char"))

    print("\n[4] 取名选男：只有男主角")
    st = fresh("p1")
    protagonist.ensure(st, 1)
    check("characterBag 只有男主角", chars(st.props) == [protagonist.male_id()], chars(st.props))
    check("choosen = 男主角", int(st.props["character_choosen_char"]) == protagonist.male_id())
    check("编队 = [男,0,0]", team_slots(st.props) == [protagonist.male_id(), 0, 0], team_slots(st.props))

    print("\n[5] 未选性别 -> 选女：只有女主角（不是「加一个」）")
    st = fresh("p2")
    protagonist.ensure(st, 2)
    check("characterBag 只有女主角", chars(st.props) == [protagonist.female_id()], chars(st.props))
    check("choosen = 女主角", int(st.props["character_choosen_char"]) == protagonist.female_id())
    check("编队 = [女,0,0]", team_slots(st.props) == [protagonist.female_id(), 0, 0], team_slots(st.props))

    print("\n[6] 旧存档双主角（男+女）：互斥自愈只留一个")
    for gender, keep, drop in ((1, protagonist.male_id(), protagonist.female_id()),
                              (2, protagonist.female_id(), protagonist.male_id())):
        st = fresh(f"legacy{gender}")
        for cid in (protagonist.male_id(), protagonist.female_id()):
            st.props["characterBag"][cid] = factory.make_character(st.props, cid)
        protagonist.ensure(st, gender)
        check(f"gender={gender} 只剩 {keep}", chars(st.props) == [keep], chars(st.props))
        check(f"gender={gender} 删掉了 {drop}", drop not in st.props["characterBag"])

    print("\n[7] 幂等：连续 ensure 第二次无帧、状态不变")
    st = fresh("idem")
    protagonist.ensure(st, 2)
    before = (json.dumps(chars(st.props)), st.props["character_choosen_char"], team_slots(st.props))
    frames = protagonist.ensure(st, 2)
    after = (json.dumps(chars(st.props)), st.props["character_choosen_char"], team_slots(st.props))
    check("第二次不产生帧", frames == [], frames)
    check("状态不变", before == after, (before, after))

    print("\n[8] 真实 RPC：RpcCommitNameGender 女 -> 客户端收到互斥结果")
    from game_server import dispatch as _dispatch
    from game_server import rpc_index
    st = fresh("rpc_f")
    pstate.STATES["rpc_f"] = st
    idx = rpc_index.RPC_INDEX["RpcCommitNameGender"]
    frames = _dispatch.handle(idx, msmod.mp_arr(["导演小姐", 2]), 9, uid="rpc_f")
    check("有同步帧", bool(frames), len(frames))
    check("包里只有女主角", chars(st.props) == [protagonist.female_id()], chars(st.props))
    check("gender=2", int(st.props.get("gender") or 0) == 2, st.props.get("gender"))
    check("player_name 写入", st.props.get("player_name") == "导演小姐", st.props.get("player_name"))
    check("客户端进街区条件满足（至少一个主角）",
          any(cid in st.props["characterBag"] for cid in protagonist.protagonist_ids()),
          chars(st.props))

    print("\n[9] 登录自愈：旧存档（双主角）在 live_init_dict 后只剩一个")
    # 造一份"旧存档"：双主角 + 编队里的男导 + gender=2
    st_old = fresh("legacy_login")
    for cid in (protagonist.male_id(), protagonist.female_id()):
        st_old.props["characterBag"][cid] = factory.make_character(st_old.props, cid)
    st_old.props["gender"] = 2
    st_old.props["character_choosen_char"] = protagonist.male_id()
    st_old.save()
    from game_server.ms import initdict as _init
    _init.live_init_dict("legacy_login")
    st_after = pstate.get_state(uid="legacy_login")
    check("登录后只剩女主角",
          sorted(k for k in st_after.props["characterBag"] if isinstance(k, int)) == [protagonist.female_id()],
          list(st_after.props["characterBag"]))
    check("choosen 对齐成女主角",
          int(st_after.props["character_choosen_char"]) == protagonist.female_id(),
          st_after.props["character_choosen_char"])
    check("编队第 1 队 = [女,0,0]",
          team_slots(st_after.props) == [protagonist.female_id(), 0, 0],
          team_slots(st_after.props))

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
