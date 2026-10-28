#!/usr/bin/env python
"""客户端契约模块（tools/client_contract.py）自身的测试。

## 为什么这个测试独立存在

tests/test_avatar_prop_coverage.py 断言的是「契约对**当前客户端**成立」。
但如果契约的**判定逻辑**本身写错了（比如推导顺序反了、provided 被忽略），
那个测试也可能全绿 —— 2026-10-28 第一版 resolve() 就是这样：
漏了 provided，报出 79 个假缺口（level / exp / street_id 全被当成缺失）。

所以这里测的是**判定本身**：给定一个读点，契约必须给出正确结论。
用假 LUA_ROOT + 假 provided 集合，不依赖真实客户端。

## 断言覆盖的四件事

  [1] 推导规则：snake / camel 两种命名都能推导，推不出返回 None
  [2] 判定顺序：provided > EXEMPT > SERVER_PROPS > SIMPLE_PROPS > 推导 > unknown
  [3] DERIVE_TRAPS 负例：推导**会错**的名字必须被 SERVER_PROPS 覆盖
  [4] required_types() 不含推导结果（防「静默错误类型」）

用法: python -m tests.test_client_contract
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import client_contract as cc  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


#: 假的客户端 Lua 目录：只有几个读点，用来验证扫描 + 判定
FAKE_LUA = {
    # 已下发的
    "level": "entities/avatarcomponent/avatarlevelcomp.lua",
    # 豁免的
    "destroyed": "net/msnetworkmanager.lua",
    # SERVER_PROPS 声明过的
    "task_bag": "entities/avatarcomponent/avatartaskcomp.lua",
    # SIMPLE_PROPS 声明过的
    "street_id": "manager/loginmanager.lua",
    # 能推导的
    "title_daily_task_cache": "ui/panel/x/node.lua",
    # 推不出任何东西的
    "some_undeclared_thing": "ui/panel/y/node.lua",
}


def make_fake_lua():
    """造一个假 Lua 目录，每个名字写进一个 .lua 文件里。

    同时写方法调用（GlobalData.Avatar.X(...) / X:Y()）进去，
    验证 PROP_RE 不会把它们当成属性读点。
    """
    root = tempfile.mkdtemp(prefix="oo_cc_lua_")
    for name, rel in FAKE_LUA.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"local v = GlobalData.Avatar.{name}\n")
            f.write(f"GlobalData.Avatar.{name}()  -- 方法调用，不是读点\n")
    # 单独一个文件：同名方法调用，没���属性读
    with open(os.path.join(root, "only_call.lua"), "w", encoding="utf-8") as f:
        f.write("GlobalData.Avatar.callOnly(x)\n")
        f.write("GlobalData.Avatar.colonOnly:foo()\n")
    return root


def run():
    fake = make_fake_lua()
    declared = {"TitleDailyTaskCache": {}, "TaskBag": {}, "TaskTypeBag": {}}
    provided = {"level"}

    print("\n[1] 扫描：只收属性式访问，不收方法调用")
    found = cc.discover(fake)
    check("扫到 6 个不同名字", len(found) == len(FAKE_LUA), sorted(found))
    check("方法调用没被当成属性读点",
          "callOnly" not in found and "colonOnly" not in found, sorted(found))
    check("读点文件名记下来了", found.get("level") == ("entities/avatarcomponent/avatarlevelcomp.lua",),
          found.get("level"))
    check("LUA_ROOT 不存在时返回空 dict，不炸",
          cc.discover(os.path.join(fake, "no_such_dir")) == {})

    print("\n[2] 推导规则：两种命名都要覆盖")
    check("snake_case -> PascalCase",
          cc.derive_type("title_daily_task_cache", declared) == "TitleDailyTaskCache",
          cc.derive_type("title_daily_task_cache", declared))
    check("camelCase -> PascalCase",
          cc.derive_type("taskBag", {"TaskBag": {}}) == "TaskBag",
          cc.derive_type("taskBag", {"TaskBag": {}}))
    check("推不出时返回 None",
          cc.derive_type("some_undeclared_thing", declared) is None,
          cc.derive_type("some_undeclared_thing", declared))
    # 记录行为：空段被丢弃，所以 "_a_b" 推导成 "AB" 而不是 "_AB"。
    # 客户端属性名没有下划线开头的实际例子，这里只是把规则写死，防以后改坏。
    check("连续下划线按空段丢弃（_a_b -> AB）",
          cc.derive_type("_a_b", {"AB": {}}) == "AB",
          cc.derive_type("_a_b", {"AB": {}}))

    print("\n[3] 判定顺序：provided > EXEMPT > SERVER_PROPS > SIMPLE_PROPS > 推导 > unknown")
    r = cc.resolve("level", found.get("level", ()), declared, provided)
    check("已下发的判为 provided", r.kind == "provided" and r.ok, (r.kind, r.reason))

    r = cc.resolve("destroyed", (), declared, provided)
    check("豁免的判为 exempt 且 ok", r.kind == "exempt" and r.ok, (r.kind, r.reason))
    check("豁免理由非空且不是空话", len(cc.EXEMPT["destroyed"]) > 5, cc.EXEMPT["destroyed"])

    # ★ 顺序：task_bag 同时能满足「推导」和「SERVER_PROPS」。
    #   必须判 server（= 已声明但没产出），而不是 derivable ——
    #   因为 SERVER_PROPS 是人确认过的结论，推导只是候选。
    r = cc.resolve("task_bag", (), declared, provided)
    check("SERP_PROPS 优先于推导", r.kind == "server" and r.type == "TaskTypeBag",
          (r.kind, r.type))

    r = cc.resolve("street_id", (), declared, provided)
    check("SIMPLE_PROPS 判为 simple（生成器 bug）", r.kind == "simple", (r.kind, r.reason))

    # 用一个**不在 SERVER_PROPS 里**的名字测推导（title_daily_task_cache 已经在里面了 ——
    # 这正是「人确认过就升级成 SERVER_PROPS」的正常结果，所以不能拿它测 derivable）。
    r = cc.resolve("pet_skin_bag", (), {"PetSkinBag": {}}, {"task_bag"})
    check("能推导的判为 derivable 且给出类型",
          r.kind == "derivable" and r.type == "PetSkinBag", (r.kind, r.type))
    # ★ 推导只做候选：title_daily_task_cache 一旦进了 SERVER_PROPS 就必须判 server，
    #   而不是继续判 derivable —— 否则「已确认」和「待确认」两种状态分不开。
    check("已在 SERVER_PROPS 的不会退回 derivable",
          cc.resolve("title_daily_task_cache", (), declared, set()).kind == "server")
    check("derivable 不算「有来源」（要人确认）", not r.ok)

    r = cc.resolve("some_undeclared_thing", (), declared, provided)
    check("什么都没有的判为 unknown 且不 ok", r.kind == "unknown" and not r.ok, r.kind)

    # ★ 顺序：provided 必须先于 EXEMPT。否则「已经下发了」会被误报成豁免，
    #   而豁免是「不需要下发」——语义相反。
    orig = dict(cc.EXEMPT)
    try:
        cc.EXEMPT["level"] = "如果这个条目生效，说明顺序错了"
        r = cc.resolve("level", (), declared, provided)
        check("provided 优先于 EXEMPT（顺序反了就不会崩、只会判错）",
              r.kind == "provided", r.kind)
    finally:
        cc.EXEMPT.clear()
        cc.EXEMPT.update(orig)

    print("\n[4] DERIVE_TRAPS：推导会错的名字必须被 SERVER_PROPS 覆盖")
    traps = cc.DERIVE_TRAPS
    check("至少有一条陷阱（否则这张表没有存在意义）", len(traps) >= 1, len(traps))
    for name, (right, why) in traps.items():
        check(f"{name} 在 SERVER_PROPS 里", name in cc.SERVER_PROPS)
        check(f"{name} 的 SERVER_PROPS 类型 == DERIVE_TRAPS 的正确类型",
              cc.SERVER_PROPS.get(name) == right,
              (cc.SERVER_PROPS.get(name), right))
        check(f"{name} 的陷阱理由足够长", len(why) > 20, len(why))
    # ★ 负例本体：task_bag 推导出来的**确实是** TaskBag（错的），
    #   说明「文件名看起来能推」这件事真的不可靠。
    check("task_bag 推导得到 TaskBag（确认陷阱真实存在）",
          cc.derive_type("task_bag", {"TaskBag": {}, "TaskTypeBag": {}}) == "TaskBag",
          cc.derive_type("task_bag", {"TaskBag": {}, "TaskTypeBag": {}}))

    print("\n[5] required_types() 不许包含推导结果")
    req = cc.required_types()
    check("required_types == SERVER_PROPS 的副本", req == cc.SERVER_PROPS, len(req))
    check("required_types 与 SERVER_PROPS 不是同一个对象（防外部改动污染）",
          req is not cc.SERVER_PROPS)
    derived_only = set()
    for n in found:
        t = cc.derive_type(n, declared)
        if t and n not in cc.SERVER_PROPS and n not in cc.EXEMPT:
            derived_only.add(n)
    check("推导出来的名字没有被偷偷加进 required_types",
          derived_only.isdisjoint(req), sorted(derived_only & set(req)))
    simple = cc.required_simple_props()
    check("required_simple_props 返回 {名字: 值} 而不是 (值, 理由)",
          simple.get("street_id") == 101 and simple.get("character_choosen_char") == 0,
          simple.get("street_id"))
    # ★ SERVER_PROPS 与 SIMPLE_PROPS 不能有交集：
    #   一个要对象、一个要标量，同时存在说明有人搞混了客户端怎么用它。
    both = sorted(set(cc.SERVER_PROPS) & set(cc.SIMPLE_PROPS))
    check("SERVER_PROPS 与 SIMPLE_PROPS 不相交", not both, both)

    print("\n[6] 契约表自身的完整性")
    empty_reason = [k for k, v in cc.EXEMPT.items() if not v.strip()]
    check("EXEMPT 每条都有理由", not empty_reason, empty_reason)
    lazy = [k for k, v in cc.EXEMPT.items()
            if "暂时" in v or "不要紧" in v or "没关系" in v]
    check("EXEMPT 理由里没有「暂时/不要紧/没关系」这类糊弄话", not lazy, lazy)
    no_reason = [k for k, v in cc.SIMPLE_PROPS.items() if not v[1].strip()]
    check("SIMPLE_PROPS 每条都有依据", not no_reason, no_reason)
    check("SIMPLE_PROPS 的默认值类型和属性名对得上（street_id 是 int）",
          isinstance(cc.SIMPLE_PROPS["street_id"][0], int)
          and not isinstance(cc.SIMPLE_PROPS["street_id"][0], bool))

    print()
    print(f"=== 结果: PASS={len(passed)} FAIL={len(failed)} ===")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print(f"通过 {len(passed)} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(run())
