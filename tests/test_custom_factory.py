#!/usr/bin/env python
"""自定义类型工厂（property/factory.py）回归测试。

背景（2026-10-23）
------------------
以前服务端有 6 处手抄的字段表（scaffold / story/state_machine /
story/__init__ / tasks / street/info / street/dispatch），加上女主角色是
「深拷贝 9001 再改皮肤号」。后者有个真 bug：9001 一旦有装备/皮肤/养成进度，
新建的 9002 会**继承**过去。

现在统一走 property.factory（字段来自 spec_data.json，config.lua 机械生成）。
本测试锁住：
  1. 字段集合 == 客户端声明（不多不少）
  2. __tp__ 规则：顶层带、嵌套不带
  3. 嵌套容器都有 __id__，且同批唯一
  4. make_character：useSkinId 取 CharData.DefaultSkin，且**不继承**源角色状态
  5. 未知类型响亮失败（不许静默产空壳）

用法: python tests/test_custom_factory.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_factory_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_factory_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402
from game_server import player_state as pstate
from game_server.property import factory, spec  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def new_state():
    return pstate.PlayerState(uid="factory_u", props={})


def fields_of(obj):
    return sorted(k for k in obj if k not in ("__id__", "__tp__"))


def nested_containers(obj):
    return [k for k, v in obj.items() if isinstance(v, dict) and "__id__" in v]


def run():
    print("\n[1] 字段集合 == 客户端声明（spec_data.json）")
    st = new_state()
    for t in ("CharacterInfo", "StoryTaskItem", "TaskItem", "ActiveTask",
              "StreetInfo", "StreetDispatchInfo", "TaskBag"):
        obj = factory.new_instance(st.props, t, with_tp=True)
        expect = sorted(spec.struct_fields(t).keys())
        check("%s 字段一致（%d 个）" % (t, len(expect)),
              fields_of(obj) == expect,
              (fields_of(obj), expect))

    print("\n[2] __tp__ 规则：顶层带、嵌套不带")
    st = new_state()
    root = factory.new_instance(st.props, "StoryTaskItem", with_tp=True)
    nested = factory.new_instance(st.props, "StoryTaskItem")
    check("顶层带 __tp__", root.get("__tp__") == "StoryTaskItem", root.get("__tp__"))
    check("嵌套不带 __tp__", "__tp__" not in nested, nested.get("__tp__"))
    ch = factory.make_character(st.props, 9002)
    check("CharacterInfo（容器值）不带 __tp__", "__tp__" not in ch, ch.get("__tp__"))

    print("\n[3] 嵌套容器都有 __id__，且同批唯一")
    st = new_state()
    for t in ("CharacterInfo", "StreetInfo", "StoryTaskItem", "TaskItem"):
        obj = factory.new_instance(st.props, t, with_tp=True)
        ks = nested_containers(obj)
        check("%s 嵌套容器都带 __id__（%d 个）" % (t, len(ks)),
              bool(ks) and all(isinstance(obj[k].get("__id__"), int) for k in ks))
        ids = [obj["__id__"]] + [obj[k]["__id__"] for k in ks]
        check("%s 同批 __id__ 唯一" % t, len(ids) == len(set(ids)), ids)

    print("\n[4] make_character：皮肤取 BDD，且不继承源角色状态")
    st = new_state()
    # 先给 9001 塞一点"养成进度"，模拟玩过的号
    st.props["characterBag"] = {
        "__tp__": "CharacterPersistentDict", "__id__": 1326,
        9001: {"__id__": 1345, "useSkinId": 900101, "charValue": 99,
               "equip_bag": {"__id__": 1351, "x": 1},
               "skins": {"__id__": 1354, 900101: {"__id__": 1999}}},
    }
    info = factory.make_character(st.props, 9002)
    cd = gamedata.char(9002) or {}
    check("useSkinId = CharData[9002].DefaultSkin",
          info.get("useSkinId") == int(cd.get("DefaultSkin") or 0),
          (info.get("useSkinId"), cd.get("DefaultSkin")))
    check("★ 不继承 charValue", info.get("charValue") == 0, info.get("charValue"))
    check("★ 不继承 skins", not info.get("skins", {}).keys() - {"__id__"},
          info.get("skins"))
    check("★ 不继承 equip_bag", not info.get("equip_bag", {}).keys() - {"__id__"},
          info.get("equip_bag"))
    check("9001 的存档没被改动",
          st.props["characterBag"][9001]["charValue"] == 99)

    print("\n[5] 未知类型要响亮失败")
    st = new_state()
    try:
        factory.new_instance(st.props, "NoSuchType_xyz")
        check("未知类型抛 ValueError", False, "没有抛")
    except ValueError as e:
        check("未知类型抛 ValueError", True, e)

    print("\n[6] 源码里不再有手抄字段表")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    banned = ("_STORY_TASK_ITEM =", "_ACTIVE_TASK =", "_INT_DICT_KEY_INT =",
              "STORY_TASK_ITEM_FIELDS =", "_TASK_ITEM =",
              "_STREET_INFO_DEFAULTS =", "def _realloc_ids(")
    hits = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "game_server")):
        if "__pycache__" in dirpath:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                src = open(p, encoding="utf-8").read()
            except Exception:
                continue
            for b in banned:
                if b in src:
                    hits.append("%s: %s" % (os.path.relpath(p, root), b))
    check("★ 手抄字段表已全部清除", not hits, hits[:5])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
