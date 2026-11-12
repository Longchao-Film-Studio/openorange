#!/usr/bin/env python3
"""角色空槽位哨兵 —— 守卫测试（角色详情页点不进去的那条根因）。

## 被测的是什么

客户端把「没装备」表示成**值为空串**（SystemConst.EmptyItemUUID = ""），
不是「键不存在」。而 avatarsigilcomp.lua:145 / avatarstampcomp.lua:61
是「按键取再和空串比」，没有 nil 判断 —— 键不存在时 nil ~= "" 为真，
客户端把空槽位当成已装备，随后拿 nil gridId 崩掉：

    avatarsigilcomp.lua:43  return #sigilBreakData   -- nil 取长度
    <- refreshSigilRedPoint <- refreshRedPoint <- refreshAll <- setData

表现就是**角色详情页点不进去**，而服务端日志完全正常。

## 断言（关键是最后一条负例）

  [1] 槽位键是从客户端 systemconst.lua 现解析的，且和源码逐字一致
  [2] 新建角色带哨兵（factory.make_character 是唯一建角色入口）
  [3] 幂等，且不覆盖已装备的 UUID
  [4] 老存档经 migrate_schema 自愈（登录即修，不用重开档）
  [5] ★ 同源 Lua：带哨兵 -> 不崩；删掉哨兵 -> 必须崩

第 [5] 条是这条修复的**唯一**证明：单元断言「服务端写了空串」证明不了
客户端就不再崩。必须让客户端自己的代码跑一遍，而且必须先看到它崩。

用法: python -m tests.test_empty_item_slots
"""
import copy
import os
import sys
import tempfile
import types

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("OO_STATE_DIR", tempfile.mkdtemp(prefix="oo_test_slots_"))
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_slots_ulog_"), "unknown_requests.tsv"))

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


def _fresh_props():
    """真登录路径造一个女号：live_init_dict -> protagonist.ensure_props。"""
    from game_server import protagonist
    from game_server.ms.initdict import live_init_dict

    init = live_init_dict(uid="t_empty_slots")
    props = init["__Properties__"]
    protagonist.ensure_props(props, 2)
    return props


def _char_cell(props):
    bag = props.get("characterBag") or {}
    for key, cell in bag.items():
        if key not in ("__tp__", "__id__"):
            return int(key), cell
    return 0, {}


def _client_slots():
    """从客户端常量表里独立解析槽位键（不复用被测实现）。"""
    import re

    from game_server.property import callback_contract

    path = os.path.join(callback_contract.lua_root(), "common", "systemconst.lua")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    out = {}
    for field, enum in (("sigil_bag", "SigilType"), ("stamp_bag", "StampType")):
        m = re.search(r"\b" + enum + r"\s*=\s*\{([^{}]*)\}", text)
        out[field] = sorted(int(v) for v in re.findall(r"=\s*(-?\d+)", m.group(1)))
    return out, text


def main():
    from game_server.property import empty_slots, factory

    print("\n[1] 槽位键来自客户端常量表，且与源码一致")
    check("能读到客户端常量表", empty_slots.available(), empty_slots.unavailable_reason())
    if not empty_slots.available():
        print(f"\n=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
        return 1
    want, src = _client_slots()
    got = {k: list(v) for k, v in empty_slots.slots().items()}
    check("解析出的槽位键与独立解析一致", got == want, f"{got} != {want}")
    check("EmptyItemUUID 是空串（客户端定义）", empty_slots.empty_uuid() == "",
          repr(empty_slots.empty_uuid()))
    check('源码里确有 SystemConst.EmptyItemUUID = ""',
          'SystemConst.EmptyItemUUID = ""' in src)
    check("源码里确有 SigilType = {First = 1}", "SigilType = {First = 1}" in src)
    check("源码里确有 StampType = {Yin = 0, Yang = 1}",
          "StampType = {Yin = 0, Yang = 1}" in src)

    print("\n[2] 新建角色带哨兵（建角色唯一入口）")
    info = factory.make_character(_fresh_props(), 9002)
    for field, keys in want.items():
        node = info.get(field) or {}
        missing = [k for k in keys if k not in node]
        check(f"{field} 槽位齐全", not missing, f"缺 {missing}")
        check(f"{field} 空槽位值是空串",
              all(node.get(k) == "" for k in keys),
              {k: node.get(k) for k in keys})

    print("\n[3] 幂等，且不覆盖已装备 UUID")
    twice = empty_slots.fill(info)
    check("第二次 fill 不再写任何键", twice == 0, twice)
    if "sigil_bag" in want:
        slot = want["sigil_bag"][0]
        info["sigil_bag"][slot] = "uuid-equipped"
        empty_slots.fill(info)
        check("已装备的 UUID 不被覆盖",
              info["sigil_bag"][slot] == "uuid-equipped",
              info["sigil_bag"].get(slot))

    print("\n[4] 老存档经 migrate_schema 自愈")
    from game_server import prop_origin

    props = _fresh_props()
    _, cell = _char_cell(props)
    for field, keys in want.items():
        for k in keys:
            cell[field].pop(k, None)
    stripped = {f: sorted(cell[f].keys() - {"__id__"}) for f in want}
    check("负例前提：槽位确实被删空了（只剩 __id__）",
          all(v == [] for v in stripped.values()), stripped)
    # migrate_schema 只依赖 st.props / st.schema 两个字段；用最小替身而不是
    # 整个 PlayerState（那里还有锁、脏标记、落盘，与本断言无关）。
    st = types.SimpleNamespace(props=props, schema=0)
    added = prop_origin.migrate_schema(st)
    check("迁移报告补了键", added >= sum(len(v) for v in want.values()), added)
    for field, keys in want.items():
        node = _char_cell(props)[1][field]
        check(f"迁移后 {field} 槽位齐全", all(k in node for k in keys),
              sorted(k for k in node if isinstance(k, int)))

    print("\n[5] ★ 同源 Lua：带哨兵不崩，删掉哨兵必须崩")
    from tools import probe_client_view as probe

    good = _fresh_props()
    char_id, cell = _char_cell(good)
    bad_props = copy.deepcopy(good)
    probe.strip_empty_slots(bad_props)

    lua_bad = probe.boot(props=bad_props)
    ok_bad, err_bad = probe.call(
        lua_bad,
        f"GlobalData.Avatar:getCharOnceRedDotState({char_id}, "
        "SystemConst.CharOnceRedDotKey.SigilTab)")
    check("删掉哨兵后客户端必须崩（证明这条断言能红）", not ok_bad, ok_bad)
    check("崩在 avatarsigilcomp 的取长度上（与实机一致）",
          "avatarsigilcomp" in str(err_bad) or "sigilBreakData" in str(err_bad),
          str(err_bad)[:200])

    lua_good = probe.boot(props=good)
    ok_wear, wear = probe.call(
        lua_good, f"tostring(GlobalData.Avatar:isCharWearSigil({char_id}))")
    check("带哨兵：isCharWearSigil 为假（空槽位不再被当成已装备）",
          ok_wear and wear == "false", f"{wear}")
    ok_rd, rd = probe.call(
        lua_good,
        f"tostring(GlobalData.Avatar:getCharOnceRedDotState({char_id}, "
        "SystemConst.CharOnceRedDotKey.SigilTab))")
    check("带哨兵：角色详情页 SigilTab 红点不抛错", ok_rd, str(rd)[:200])

    print()
    print(f"=== 结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        return 1
    print(f"通过 {_ok} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
