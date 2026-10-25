#!/usr/bin/env python
"""P0-B：属性声明表 / 变更集 / 序列化器 测试。

覆盖：
  B1 spec.py   —— 属性声明（从 config.lua 生成 + initDict 补齐）
  B2 tx.py     —— 路径化变更集，自动决定 OP 类型和 propertyId
  B3 sync.py   —— 排序、私有字段剥离、断言

关键回归（都是审计实锤会崩客户端的场景）：
  - 容器新键必须 OP_ADD（之前 5 处用了 OP_CHANGE -> 客户端 oldVal:customTypeName() 索引 nil）
  - 结构体字段必须 OP_CHANGE（不能用 OP_ADD）
  - 混合类型（StreetInfoDict）要按「key 是不是声明字段」区分
  - 嵌套的 _oo_* 必须剥离（之前只过滤顶层，嵌套层发给客户端会 assert 崩）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_tx_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server.property import spec, tx as txmod, sync
from game_server.property.tx import OP_ADD, OP_CHANGE, OP_DEL

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] B1 属性声明表")
    s = spec.stats()
    check("根属性 218 个（173 声明 + 45 initDict）",
          s["root_props"] == 218 and s["root_props_declared"] == 173
          and s["root_props_initdict_only"] == 45, s)
    check("自定义类型 345 个", s["custom_types"] == 345, s["custom_types"])
    check("简单类型 45 / 结构体 70 / int容器 53 / list 3 / str容器 2",
          s["root_prop_kinds"] == {"simple": 45, "struct": 70, "list": 3,
                                   "int": 53, "str": 2, "unknown": 0},
          s["root_prop_kinds"])

    p = spec.get("item_bag")
    check("item_bag 是 ItemBag / 字符串键",
          p and p.type == "ItemBag" and p.key_kind == "str",
          (p.type, p.key_kind) if p else None)
    p = spec.get("story_task_bag")
    check("story_task_bag 是 StoryTaskBag / 整数键",
          p and p.type == "StoryTaskBag" and p.key_kind == "int")
    p = spec.get("char_list")
    check("char_list 是 IntList / list 键",
          p and p.key_kind == "list")
    p = spec.get("street_info")
    check("street_info 是混合类型（struct + container 都为真）",
          p and p.is_struct and p.is_container and p.key_kind == "int",
          (p.is_struct, p.is_container, p.key_kind) if p else None)
    check("spec.get 不存在的属性返回 None", spec.get("no_such_prop") is None)

    print("\n[2] B2 根属性 -> OP_CHANGE pid=0")
    props = {"street_id": 101,
             "street_info": {"__tp__": "StreetInfoDict", "__id__": 1400,
                             "all_prestige_exp": 0}}
    t = txmod.Tx(props)
    t.set(("street_id",), 1011)
    op = t.ops[-1]
    check("根属性 optype=OP_CHANGE", op.optype == OP_CHANGE, op.optype)
    check("根属性 propertyId=0", op.property_id == 0, op.property_id)
    check("props 同步改了", props["street_id"] == 1011, props["street_id"])

    print("\n[3] ★ 容器新键 -> OP_ADD（审计实锤的崩溃点）")
    t.set(("street_info", 1011), {"__id__": 1500, "prestige_level": 0})
    op = t.ops[-1]
    check("新键 optype=OP_ADD", op.optype == OP_ADD, op.optype)
    check("propertyId=父对象 __id__", op.property_id == 1400, op.property_id)
    check("name=键名", op.name == "1011", op.name)

    print("\n[4] 容器已有键 -> OP_CHANGE")
    t.set(("street_info", 1011), {"__id__": 1500, "prestige_level": 0})
    check("已有键 optype=OP_CHANGE", t.ops[-1].optype == OP_CHANGE,
          t.ops[-1].optype)

    print("\n[5] ★ 结构体字段 -> 永远是 OP_CHANGE")
    t.set(("street_info", 1011, "prestige_level"), 1)
    op = t.ops[-1]
    check("结构体字段 optype=OP_CHANGE", op.optype == OP_CHANGE, op.optype)
    check("propertyId=对象自己的 __id__", op.property_id == 1500, op.property_id)
    # 即使字段当前不存在，也不能用 OP_ADD
    t.set(("street_info", 1011, "cd_choose"), {})
    check("结构体新字段也是 OP_CHANGE", t.ops[-1].optype == OP_CHANGE,
          t.ops[-1].optype)

    print("\n[6] ★ 混合类型：自身字段 vs 字典键")
    t.set(("street_info", "all_prestige_exp"), 5)
    op = t.ops[-1]
    check("StreetInfoDict 自身字段 -> OP_CHANGE",
          op.optype == OP_CHANGE and op.property_id == 1400,
          (op.optype, op.property_id))

    print("\n[7] 删除容器键 -> OP_DEL")
    # 注意：cd_choose 是 StreetInfo 的**结构体字段**，不能删（新守卫会拦）。
    # 要删的必须是容器里的键，例如 treasure_box_info[101]。
    t.set(("street_info", 1011, "treasure_box_info"), {"__id__": 1600})
    t.set(("street_info", 1011, "treasure_box_info", 101), 1)
    t.delete(("street_info", 1011, "treasure_box_info", 101))
    op = t.ops[-1]
    check("容器键 optype=OP_DEL", op.optype == OP_DEL, op.optype)
    check("删除后 props 里没了",
          101 not in (props["street_info"][1011]["treasure_box_info"] or {}))

    print("\n[7b] ★ 守卫：不能删结构体字段（审计 P1-1）")
    try:
        t.delete(("street_info", 1011, "cd_choose"))
        check("删结构体字段被拦", False)
    except Exception as e:
        check("删结构体字段被拦", "结构体字段" in str(e), str(e))

    print("\n[7c] ★ list 键删除用 OP_REMOVE（审计 P1-1）")
    props_l = {"char_list": {"__id__": 100, 1: 9001, 2: 9002}}
    t_l = txmod.Tx(props_l)
    t_l.delete(("char_list", 1))
    check("list 删除 optype=OP_REMOVE",
          t_l.ops[-1].optype == 5, t_l.ops[-1].optype)

    print("\n[8] 自动分配 __id__")
    t.set(("street_info", 2000), {"prestige_level": 0})
    nid = props["street_info"][2000].get("__id__")
    check("新对象自动带 __id__", isinstance(nid, int), nid)
    check("分配的 id 大于已有最大值", nid > 1500, nid)

    print("\n[9] B3 排序：先父后子（★ 审计指出原测试没有顺序断言）")
    props2 = {"story_task_bag": {"__tp__": "StoryTaskBag", "__id__": 100},
              "signature": ""}
    t2 = txmod.Tx(props2)
    # 调用顺序：先建父对象，再改子字段，最后改根属性 —— ops 顺序是 [2,3,1]
    t2.set(("story_task_bag", 1000), {"__id__": 500, "state": 0})
    t2.set(("story_task_bag", 1000, "state"), 3)
    t2.set(("signature",), "x")
    raw_lens = [len(op.path) for op in t2.ops]
    check("调用顺序确实是 [2,3,1]", raw_lens == [2, 3, 1], raw_lens)
    ordered = sorted(t2.ops, key=sync._sort_key)
    lens = [len(op.path) for op in ordered]
    check("排序后路径长度单调不减（先父后子）",
          lens == sorted(lens), lens)
    check("最短的根属性排第一", lens[0] == 1, lens)
    errs = sync.validate(t2.ops)
    check("校验通过", not errs, errs)

    print("\n[9b] ★ validate 抓得住审计列的崩溃场景（P0-3）")
    # (a) OP_ADD propertyId=0
    bad1 = [txmod.Op(0, txmod.OP_ADD, "k", {"__id__": 1}, ("x", "k"))]
    check("抓到 OP_ADD pid=0", bool(sync.validate(bad1)), sync.validate(bad1))
    # (b) 同一批里 __id__ 重复
    bad2 = [txmod.Op(1, txmod.OP_ADD, "a", {"__id__": 77}, ("c", "a")),
            txmod.Op(1, txmod.OP_ADD, "b", {"__id__": 77}, ("c", "b"))]
    check("抓到 __id__ 重复", any("重复" in e for e in sync.validate(bad2)),
          sync.validate(bad2))
    # (c) 嵌套 dict 没 __id__
    bad3 = [txmod.Op(1, txmod.OP_ADD, "a",
                     {"__id__": 5, "sub": {"no": "id"}}, ("c", "a"))]
    check("抓到嵌套 dict 没 __id__",
          any("没有 __id__" in e for e in sync.validate(bad3)),
          sync.validate(bad3))

    print("\n[10] ★ 嵌套私有键剥离（_oo_* 和 __tp__）")
    # 用**真实存在的**根属性（审计指出原来用了不存在的 "nested"，那种帧
    # 在真实客户端会 assert(customType ~= nil) 崩）
    props3 = {"street_info": {"__tp__": "StreetInfoDict", "__id__": 1400}}
    t3 = txmod.Tx(props3)
    t3.set(("street_info", 2000),
           {"__tp__": "StreetInfo", "__id__": 9, "prestige_level": 1,
            "_oo_hidden": 2, "exploration_info": {"__id__": 10, "_oo_x": 3}})
    f = t3.frames()
    check("帧里没有 _oo_hidden", b"_oo_hidden" not in f[0])
    check("帧里没有 _oo_x（嵌套层）", b"_oo_x" not in f[0])
    check("★ 嵌套值里没有 __tp__（审计 P0-1/P0-2）",
          b"__tp__" not in f[0])
    check("嵌套值里保留 __id__", b"__id__" in f[0])
    check("原 props 仍保留（只剥离发送副本）",
          "_oo_hidden" in props3["street_info"][2000])

    print("\n[10b] ★ 根属性值保留 __tp__")
    props_r = {"base_prop": {"__tp__": "BaseProp", "__id__": 50}}
    t_r = txmod.Tx(props_r)
    t_r.set(("base_prop",), {"__tp__": "BaseProp", "__id__": 50, "x": 1})
    f_r = t_r.frames()
    check("根属性值保留 __tp__", b"__tp__" in f_r[0])

    print("\n[11] 校验能抓出错误")
    props4 = {"bag": {"__id__": 7}}
    t4 = txmod.Tx(props4)
    t4.set(("bag", "k"), 123)          # 值不是 dict -> OP_ADD 缺 __id__
    errs = sync.validate(t4.ops)
    check("OP_ADD 值没 __id__ 被抓出", bool(errs), errs)
    try:
        t4.frames(strict=True)
        check("strict 模式抛异常", False)
    except Exception:
        check("strict 模式抛异常", True)

    print("\n[12] ServerOnly 根属性不许推")
    # entityType 在 config.lua 里声明为 ServerOnly
    p = spec.get("entityType")
    check("entityType 声明为 ServerOnly",
          p is not None and p.is_server_only, p.scope if p else None)
    t5 = txmod.Tx({"entityType": "Avatar"})
    try:
        t5.set(("entityType",), "X")
        check("推 ServerOnly 属性抛异常", False)
    except Exception:
        check("推 ServerOnly 属性抛异常", True)

    print("\n[13] 路径错误要有清晰报错")
    t6 = txmod.Tx({"a": 1})
    try:
        t6.set(("no_such", "k"), 1)
        check("不存在的根属性抛异常", False)
    except Exception as e:
        check("不存在的根属性抛异常", "不存在" in str(e), str(e))

    print("\n[14] ★ key_kind 用客户端类定义判定（审计 P1-4）")
    # 审计实测：按「类型名以 List 结尾」猜，342 个类型里错 7 个
    for t, want in (("ActivityCheckinRecords", "list"),
                    ("GachaRecord", "list"),
                    ("GachaRewards", "list"),
                    ("Signs", "list"),
                    ("ChampionshipScheduleInfoList", "int"),
                    ("CharShownList", "int"),
                    ("EndlessRankInfoRecordList", "int")):
        got = spec.key_kind(t)
        check("%s -> %s" % (t, want), got == want, got)
    check("ItemBag 仍是 str 键", spec.key_kind("ItemBag") == "str")
    check("StoryTaskBag 仍是 int 键", spec.key_kind("StoryTaskBag") == "int")

    print("\n[15] ★ 4 层路径类型推导（审计 P1-3）")
    props4 = {"street_info": {
        "__tp__": "StreetInfoDict", "__id__": 1400,
        1011: {"__tp__": "StreetInfo", "__id__": 1500,
               "inter_actor_info": {"__id__": 1600}}}}
    t4 = txmod.Tx(props4)
    t4.set(("street_info", 1011, "inter_actor_info", 40004), 400041)
    op4 = t4.ops[-1]
    check("4 层路径 propertyId = inter_actor_info 的 __id__",
          op4.property_id == 1600, op4.property_id)
    check("4 层新键 optype=OP_ADD", op4.optype == OP_ADD, op4.optype)

    print("\n[16] ★ spec_data.json 与 config.lua 一致（审计 P1-6）")
    import subprocess
    _gen = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools", "gen_property_spec.py")
    if os.path.exists(_gen):
        rc = subprocess.call([sys.executable, _gen, "--check"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        check("生成器 --check 退出码 0（未过期）", rc == 0, rc)
    else:
        check("生成器存在", False, _gen)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
