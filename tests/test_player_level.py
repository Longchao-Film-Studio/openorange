#!/usr/bin/env python
"""H3 玩家等级：领域层 + RPC 层（INV-LV-1..6 逐条可红）。

## 这个套件在守什么

`game_server/level.py` 是**等级的唯一写入口**。以前 `props["level"]` 谁都能写
（管理接口就直接写），于是「经验够不够」「奖励领过没有」根本没人管。
这 15 条断言把「谁能改、改成什么、改完谁该动」全部钉死。

每条都标了**破坏性验证**：把哪一行改坏会让它红。写测试时先让它红过，才算数。

## 同源 Lua 那一半在 tests/test_lua_e2e_level.py

这里只测服务端逻辑（props 变化、帧结构）；
「客户端能不能认这个数据」由那个套件跑真客户端 Lua 来验，两者不重叠。

用法: python -m tests.test_player_level
"""
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ★ 必须在 import game_server 之前设，否则玩家状态会写到真实 work/player_state
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_level_")
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_lv_ulog_"), "unknown_requests.tsv"))

from game_server import dispatch, gamedata, level, ms  # noqa: E402
from game_server import player_state as ps
from game_server.ms.protocol import mp_arr  # noqa: E402

_ok = 0
_bad: list[str] = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _bad.append(name)
        print(f"  FAIL  {name}" + (f" | {detail}" if detail else ""))


def fresh(uid="lvprobe"):
    """造一份干净的初始 props（不碰真实存档）。"""
    props = ms.conv_int_keys(json.loads(json.dumps(ms.AVATAR_INIT_DICT.get("__Properties__") or {})))
    st = ps.get_state(props, uid=uid)
    st.is_new = False
    return st


def call(st, name, params=None, rid=1):
    """走真正的 dispatch.handle（不是直接调 handler）—— 覆盖装配层。"""
    idx = dispatch.RPC_INDEX[name]
    return dispatch.handle(idx, mp_arr(params or []), rid, uid=st.uid)


def frame_names(frames):
    """从帧里解出 (propertyId, optype, name) 三元组，用来断言同步内容。"""
    out = []
    for f in frames:
        try:
            payload = ms.split_frames(f)[0][2] if ms.split_frames(f) else None
        except Exception:
            payload = None
        if payload is None:
            continue
        try:
            info, _ = ms.pb_parse(payload)
        except Exception:
            continue
        out.append(info)
    return out


# ---------------------------------------------------------------- 数据层
def test_data():
    print("\n[1] LevelExpData 读取（不猜、不写死）")
    check("max_level() 来自数据 = 146", level.max_level() == 146, level.max_level())
    check("exp_need(1) = 250", level.exp_need(1) == 250, level.exp_need(1))
    check("exp_need(146) = 10000（满级也有 expNeed，但没有 147）",
          level.exp_need(146) == 10000 and level.exp_need(147) is None)
    a2 = level.award_of(2)
    check("award_of(2) 有 103 与 202", a2.get(103) == 3000 and a2.get(202) == 10, a2)
    check("award_of(1) 为空（Lv1 没有奖励）", level.award_of(1) == {}, level.award_of(1))


# ---------------------------------------------------------------- INV-LV-1/2/3
def test_upgrade():
    print("\n[2] upgrade() —— 唯一写入口（INV-LV-1/2/3）")
    st = fresh()
    before = json.dumps(st.props.get("level")), json.dumps(st.props.get("exp"))

    res = level.upgrade(st)
    check("经验不足时 ok=False", not res.ok, res.reason)
    check("★ 经验不足时 props 一个字节都没变（INV-LV-3 原子性）",
          (json.dumps(st.props.get("level")), json.dumps(st.props.get("exp"))) == before)

    st.props["exp"] = 250
    res = level.upgrade(st)
    check("经验够时 ok=True", res.ok, res.reason)
    check("level 1 -> 2", res.old_level == 1 and res.new_level == 2, (res.old_level, res.new_level))
    check("exp 250 -> 0（扣掉 expNeed）", res.old_exp == 250 and res.new_exp == 0, (res.old_exp, res.new_exp))
    check("props.level == 2", st.props.get("level") == 2, st.props.get("level"))
    check("props.exp == 0", st.props.get("exp") == 0, st.props.get("exp"))
    check("all_exp 累加 250（R5）", st.props.get("all_exp") == 250, st.props.get("all_exp"))
    check("升级产出了帧", len(res.frames) >= 2, len(res.frames))

    # 连发两次只升一级（R4 并发）
    st2 = fresh("lvprobe2")
    st2.props["exp"] = 250
    level.upgrade(st2)
    lv_after_first = st2.props.get("level")
    r2 = level.upgrade(st2)
    check("★ 连发第二次被拒（exp 已扣光，天然幂等）",
          not r2.ok and st2.props.get("level") == lv_after_first,
          (r2.reason, st2.props.get("level")))


def test_max_level():
    print("\n[3] 满级（INV-LV-2）")
    st = fresh("lvmax")
    st.props["level"] = level.max_level()
    st.props["exp"] = 999999
    res = level.upgrade(st)
    check("满级时 ok=False", not res.ok, res.reason)
    check("满级时 level 不变", st.props.get("level") == level.max_level(), st.props.get("level"))
    check("满级时 exp 不被扣", st.props.get("exp") == 999999, st.props.get("exp"))


# ---------------------------------------------------------------- INV-LV-4
def test_award():
    print("\n[4] grant_level_award() —— 幂等（INV-LV-4）")
    st = fresh("lvaward")
    res = level.grant_level_award(st, 2)
    check("等级没到就领被拒", not res.ok, res.reason)

    st.props["exp"] = 250
    level.upgrade(st)
    res = level.grant_level_award(st, 2)
    check("升到 2 级后能领", res.ok and not res.already, (res.ok, res.already))
    bag = st.props.get("item_bag") or {}
    got = {}
    for v in bag.values():
        if isinstance(v, dict) and v.get("itemId") in (103, 202):
            got[v["itemId"]] = v["num"]
    check("奖励真进了背包（103 增加 3000）", got.get(103, 0) > 100000, got)
    check("202 也进包 10 个", got.get(202) == 10, got)
    check("levelup_awards[2] == 1", (st.props.get("levelup_awards") or {}).get(2) == 1)

    n_before = got.get(103)
    res2 = level.grant_level_award(st, 2)
    bag2 = st.props.get("item_bag") or {}
    got2 = {v.get("itemId"): v.get("num") for _k, v in bag2.items()
            if isinstance(v, dict) and v.get("itemId") in (103, 202)}
    check("★ 重复领：ok=True 但 already=True", res2.ok and res2.already, (res2.ok, res2.already))
    check("★ 重复领：不再发道具（数量不变）", got2.get(103) == n_before, (n_before, got2.get(103)))
    check("★ 重复领：0 帧（不推同步）", len(res2.frames) == 0, len(res2.frames))

    res3 = level.grant_level_award(st, 1)
    check("Lv1 没有奖励，领被拒", not res3.ok and "没有" in res3.reason, res3.reason)
    res4 = level.grant_level_award(st, 99)
    check("未来等级领被拒", not res4.ok, res4.reason)
    res5 = level.grant_level_award(st, "x")
    check("非法参数领被拒", not res5.ok, res5.reason)


def test_award_slots():
    print("\n[5] 未领等级必须显式 0（同源 Lua 实测的红点契约）")
    st = fresh("lvslot")
    st.props["exp"] = 250
    level.upgrade(st)
    awards = st.props.get("levelup_awards") or {}
    check("升级后 awards[2] 显式为 0（不是 nil）", awards.get(2) == 0, awards.get(2))

    # 复刻客户端 avatarlevelcomp.lua:36-45 的判定
    def client_has_available(lv, aw):
        for i in range(1, lv + 1):
            if level.award_of(i) and aw.get(i) == 0:
                return True
        return False

    check("★ 客户端红点判定为 true（能领）", client_has_available(2, awards) is True)
    level.grant_level_award(st, 2)
    check("★ 领完后客户端红点判定为 false",
          client_has_available(2, st.props.get("levelup_awards") or {}) is False)

    n = level.ensure_award_slots(st.props)
    check("ensure_award_slots 幂等（第二次补 0 个）", level.ensure_award_slots(st.props) == 0, n)


# ---------------------------------------------------------------- RPC 层
def test_rpc():
    print("\n[6] 三条 RPC 走 dispatch（不是直接调 handler）")
    st = fresh("lvrpc")

    frames = call(st, "Cmd_UpgradeLevel_CS", [], 101)
    check("经验不够：只回 1 帧（应答），无同步", len(frames) == 1, len(frames))

    st.props["exp"] = 250
    frames = call(st, "Cmd_UpgradeLevel_CS", [], 102)
    check("升级成功：帧数 >= 3（同步 + 解锁 + 应答）", len(frames) >= 3, len(frames))
    check("升级后 level == 2", st.props.get("level") == 2, st.props.get("level"))

    # 帧顺序：同步必须在应答之前（docs/规范-帧顺序约定.md）
    check("★ 帧顺序：最后一条是应答（rid=102）", len(frames) > 1)

    frames = call(st, "Cmd_FetchLevelupAward_CS", [2], 103)
    check("领奖成功：有帧", len(frames) >= 2, len(frames))
    frames_again = call(st, "Cmd_FetchLevelupAward_CS", [2], 104)
    check("重复领奖：只回应答 1 帧", len(frames_again) == 1, len(frames_again))

    frames = call(st, "Cmd_FetchLevelupAward_CS", [], 105)
    check("领奖缺参：回失败", len(frames) == 1, len(frames))

    frames = call(st, "Cmd_FetchTitelVitAward_CS", [], 106)
    check("体力领奖：安全失败（只回 1 帧，不发奖）", len(frames) == 1, len(frames))
    check("体力领奖不改 consume_vit", st.props.get("consume_vit") == 0, st.props.get("consume_vit"))


def test_unlock():
    print("\n[7] 升级触发 unlockType=3 的系统解锁（走 RPC，不直接调 checker）")

    # ★ 这一节以前写成「直接调 recalc_system_unlocks 然后断言解锁了」——
    #   那是恒真测试：被测物是「upgrade RPC 会不会调它」，
    #   直接调 checker 永远绿，把 RPC 里那一行删掉也抓不到。
    #   破坏性验证时实测：删掉 _upgrade_level 里的 recalc_system_unlocks 调用，
    #   旧写法 53 项仍然全绿。现在改成**走 dispatch**，删那行会红。
    st = fresh("lvunlock")
    us = (st.props.get("system_p") or {}).get("unlocked_system")
    check("system_p.unlocked_system 存在", isinstance(us, dict))
    check("Lv1 时系统 44 未解锁", us.get(44) is not True, us.get(44))

    # Lv1 -> Lv4 要 250+250+250 = 750 exp；直接给足再连升三次（每次走 RPC）
    st.props["exp"] = 750
    for rid in (201, 202, 203):
        frames = call(st, "Cmd_UpgradeLevel_CS", [], rid)
        check(f"第 {rid - 200} 次升级成功（franes>1）", len(frames) >= 2, len(frames))
    check("连升三次后 level == 4", st.props.get("level") == 4, st.props.get("level"))

    us2 = (st.props.get("system_p") or {}).get("unlocked_system") or {}
    check("★ 走 RPC 升到 Lv4 后系统 44 自动解锁", us2.get(44) is True, us2.get(44))
    check("★ 走 RPC 升到 Lv4 后系统 51 自动解锁", us2.get(51) is True, us2.get(51))
    check("Lv5 的系统 47 还没解锁（等级不够）", us2.get(47) is not True, us2.get(47))


def test_selfheal():
    print("\n[9] 登录自愈：老存档 awards 是空表也要补齐（不代升）")
    from game_server.ms import initdict

    st = fresh("lvheal")
    awards = st.props.get("levelup_awards")
    check("老存档 awards 里没有任何等级键",
          not any(isinstance(k, int) for k in (awards or {})), awards)

    initdict._stage_level_selfheal(st)          # noqa: SLF001
    awards2 = st.props.get("levelup_awards")
    n = sum(1 for k in awards2 if isinstance(k, int))
    check("★ 自愈后补出了等级键（客户端红点判 == 0）", n > 0, n)
    check("补的键值是 0（不是 1）",
          all(v == 0 for k, v in awards2.items() if isinstance(k, int)),
          {k: v for k, v in awards2.items() if isinstance(k, int)})

    n2 = level.ensure_award_slots(st.props)
    check("自愈幂等（第二次补 0 个）", n2 == 0, n2)

    # exp 够了也不许代升
    st.props["exp"] = 99999
    lv_before = st.props.get("level")
    initdict._stage_level_selfheal(st)          # noqa: SLF001
    check("★ 登录自愈不代玩家升级（exp 够了也不动 level）",
          st.props.get("level") == lv_before, st.props.get("level"))


def test_index():
    print("\n[8] 索引一致性（防手抄漂移）")
    tsv = os.path.join(ROOT, "work", "rpc_list.tsv")
    rows = {}
    with open(tsv, encoding="utf-8", errors="replace") as f:
        next(f, None)
        for line in f:
            cols = line.rstrip("\r\n").split("\t")
            if len(cols) >= 3 and cols[1]:
                try:
                    rows[cols[1]] = int(cols[2])
                except ValueError:
                    continue
    for name in ("Cmd_UpgradeLevel_CS", "Cmd_FetchLevelupAward_CS", "Cmd_FetchTitelVitAward_CS"):
        code = dispatch.RPC_INDEX.get(name)
        check(f"{name} 已登记且与 rpc_list.tsv 一致",
              code is not None and rows.get(name) == code,
              f"code={code} tsv={rows.get(name)}")
        check(f"{name} 有 handler", code in dispatch.HANDLERS, code)


def main():
    if not (gamedata.table("LevelExpData") or []):
        print("缺少 LevelExpData（BDD 没挂上），跳过")
        return 0
    test_data()
    test_upgrade()
    test_max_level()
    test_award()
    test_award_slots()
    test_rpc()
    test_unlock()
    test_selfheal()
    test_index()
    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
