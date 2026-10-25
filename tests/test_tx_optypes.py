#!/usr/bin/env python
"""B 段「变更集框架」6 项测试的正式用例（P0-B 收尾）。

对应 docs/P0-地基.md B 段「测试」那 6 条：
  1. 每个 optype 都有用例（ADD / CHANGE / DEL / INSERT / REMOVE）
  2. 新键必用 OP_ADD（每个容器一个用例）
  3. 嵌套容器新建用例（container_add -> child_add 顺序正确）
  4. 键类型转换用例（ItemBag 字符串 / StoryTaskBag 整数 / IntList #N）
  5. _oo_* 不出现在帧里
  6. 回归：发奖后客户端第 1 格不被覆盖

★ 第 1 条以前补不出来，因为 OP_INSERT 服务端从来没有产出路径：
  Tx 只有 set/delete，list 属性只能整表替换。本次给 Tx 加了
  insert / append / remove_at，OP_INSERT 才第一次有真实生产者。

用法: python tests/test_tx_optypes.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_txopt_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, player_state as pstate, story
from game_server.property import ids as _ids
from game_server.property import tx as _tx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_cache import frames_safe

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def state(uid):
    msmod._live_init_dict(uid)
    return pstate.get_state(uid=uid)


def op_of(frame):
    # 解一条 propertySync 帧 -> (propertyId, optype, name, value)
    fields = msmod.pb_parse(bytes(frame[6:]))
    pid = fields.get(2, [0])[0]
    op = fields.get(3, [0])[0]
    raw_name = fields.get(4, [b""])[0]
    name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else raw_name
    raw = fields.get(5, [b""])[0]
    val = msmod.mp_decode(raw)[0] if raw else None
    return pid, op, name, val

def run():
    print("\n[1] 每个 optype 都有用例（ADD / CHANGE / DEL / INSERT / REMOVE）")
    st = state("txopt_a")
    bag = st.props["item_bag"]
    bag_id = bag["__id__"]

    # CHANGE：改根属性（客户端 initDict 里一定有这个根）
    t = _tx.Tx(st.props)
    t.set(("street_id",), 1011)
    pid, op, name, val = op_of(t.frames(strict=True)[0])
    check("根属性 -> OP_CHANGE", op == _tx.OP_CHANGE and pid == 0, (pid, op))

    # ADD：容器里加新键
    t = _tx.Tx(st.props)
    t.set(("item_bag", "90"), {"__tp__": "ItemInfo", "id": "90",
                              "itemId": 999, "num": 1,
                              "expireTime": 0.0, "getTime": 0.0})
    pid, op, name, val = op_of(t.frames(strict=True)[0])
    check("新键 -> OP_ADD", op == _tx.OP_ADD and pid == bag_id and name == "90",
          (pid, op, name))

    # DEL：删容器里已有的键
    t = _tx.Tx(st.props)
    t.delete(("item_bag", "90"))
    pid, op, name, val = op_of(t.frames(strict=True)[0])
    check("删字典键 -> OP_DEL", op == _tx.OP_DEL and pid == bag_id, (pid, op))

    # INSERT：往 list 插元素（本次新增的 Tx.insert）
    t = _tx.Tx(st.props)
    t.append(("char_list",), 9001)
    pid, op, name, val = op_of(t.frames(strict=True)[0])
    check("list 插入 -> OP_INSERT", op == _tx.OP_INSERT, (pid, op, name))
    check("OP_INSERT 的 propertyId = list 的 __id__",
          pid == st.props["char_list"]["__id__"], pid)
    check("OP_INSERT 的 name 是 1-based 位置", name == "1", name)

    # REMOVE：从 list 删元素
    t = _tx.Tx(st.props)
    t.remove_at(("char_list",), 1)
    pid, op, name, val = op_of(t.frames(strict=True)[0])
    check("list 删除 -> OP_REMOVE", op == _tx.OP_REMOVE, (pid, op, name))
    check("OP_REMOVE 不带值", val is None, val)

    print("\n[2] 新键必用 OP_ADD（每个容器一个用例）")
    cases = [
        ("item_bag", "901", {"__tp__": "ItemInfo", "id": "901", "itemId": 998,
                             "num": 1, "expireTime": 0.0, "getTime": 0.0}),
        ("story_task_bag", 9001, {"__tp__": "StoryTaskItem", "id": 9001,
                                  "state": 3, "get_time": 0, "start_time": 0}),
        ("street_info", 1011, {"__tp__": "StreetInfo"}),
        ("characterBag", 9500, {"__tp__": "CharacterInfo"}),
        ("battle_teach_state", 777, 0),
    ]
    for root, key, value in cases:
        container = st.props.get(root)
        if not isinstance(container, dict):
            check("%s 容器存在" % root, False, type(container))
            continue
        t = _tx.Tx(st.props)
        t.set((root, key), value)
        pid, op, name, _v = op_of(t.frames(strict=True)[0])
        check("%s 新键 %r -> OP_ADD" % (root, key), op == _tx.OP_ADD,
              (pid, op, name))
        check("%s 的 propertyId = 容器 __id__" % root,
              pid == container.get("__id__"), (pid, container.get("__id__")))
        # 同一个键再改一次 -> 必须是 OP_CHANGE
        t2 = _tx.Tx(st.props)
        t2.set((root, key), value)
        _pid, op2, _n2, _v2 = op_of(t2.frames(strict=True)[0])
        check("%s 已有键 %r -> OP_CHANGE" % (root, key), op2 == _tx.OP_CHANGE, op2)

    print("\n[3] 嵌套容器新建：先父后子（container_add -> child_add）")
    st3 = state("txopt_b")
    t = _tx.Tx(st3.props)
    t.set(("task_bag", 4), {"__tp__": "TaskBag"})
    # ★ 用槽位 4（TaskType.Suspend，数据里 0 条任务 -> 登录播种不会建它）
    #   阶段 N-a2 之后登录会播种通用任务，槽位 1/3/5/9/10 都已存在，
    #   而这一项测的是「嵌套容器新建：先父后子」，必须用不存在的槽位。
    slot_id = st3.props["task_bag"][4]["__id__"]
    # ★ 同一个 Tx 里往刚建的父容器塞子对象：父帧的 value 是 props 的活引用，
    #   已经带着这个子对象了，所以**不能再给子对象单独发一条 OP_ADD**
    #   （否则客户端收到两遍同一个 __id__ -> "id repeated!" 并覆盖缓存）。
    # 嵌套的自定义属性必须带 __id__（客户端 roinit 会 assert）；生产代码
    # tasks.make_task_item 也是这么手工补的，Tx 只保证顶层对象有 __id__。
    t.set(("task_bag", 4, 1001), {"__tp__": "TaskItem", "id": 1001,
                                  "state": 3, "get_time": 0,
                                  "complete_count": 0,
                                  "cache": {"__id__": _ids.alloc_id(st3.props)}})
    frames = t.frames(strict=True)
    check("父容器 OP_ADD 时，子对象不再单独发帧", len(frames) == 1, len(frames))
    pid0, op0, name0, v0 = op_of(frames[0])
    check("这一帧是父容器 OP_ADD", op0 == _tx.OP_ADD and name0 == "4",
          (pid0, op0, name0))
    check("父帧的 propertyId 指向 task_bag 根容器",
          pid0 == st3.props["task_bag"]["__id__"], pid0)
    check("父帧的 value 里已经带着新子对象",
          isinstance(v0, dict) and any(str(k) == "1001" for k in v0),
          list(v0.keys())[:5] if isinstance(v0, dict) else v0)
    # 父容器已存在时，再往里面加子对象 -> 子对象自己发 OP_ADD，pid 指向父
    t2 = _tx.Tx(st3.props)
    t2.set(("task_bag", 4, 1002), {"__tp__": "TaskItem", "id": 1002,
                                   "state": 3, "get_time": 0,
                                   "complete_count": 0,
                                   "cache": {"__id__": _ids.alloc_id(st3.props)}})
    frames2 = t2.frames(strict=True)
    check("父已存在时子对象单独发 1 帧", len(frames2) == 1, len(frames2))
    pid1, op1, name1, _v1 = op_of(frames2[0])
    check("子帧是 OP_ADD 且 name=1002", op1 == _tx.OP_ADD and name1 == "1002",
          (pid1, op1, name1))
    check("子帧的 propertyId 指向父容器", pid1 == slot_id, (pid1, slot_id))
    # 排序：同一 Tx 里父层 op 必须排在子层 op 之前（客户端要先拿到父的 __id__）
    tpre = _tx.Tx(st3.props)
    tpre.set(("street_info", 1011), {"__tp__": "StreetInfo"})
    tpre.frames()
    t3 = _tx.Tx(st3.props)
    t3.set(("street_info", 1011, "prestige_level"), 3)
    t3.set(("task_bag", 2), {"__tp__": "TaskBag"})
    frames3 = t3.frames(strict=True)
    check("深度不同的 op 都会发出来", len(frames3) == 2, len(frames3))
    _pa, _oa, na, _va = op_of(frames3[0])
    _pb, _ob, nb, _vb = op_of(frames3[1])
    check("排序：浅层（父）帧排在深层（子）帧之前",
          na == "2" and nb == "prestige_level", (na, nb))
    print("\n[4] 键类型转换（ItemBag 字符串 / StoryTaskBag 整数 / IntList #N）")
    st4 = state("txopt_c")
    t = _tx.Tx(st4.props)
    t.set(("item_bag", "7"), {"__tp__": "ItemInfo", "id": "7", "itemId": 997,
                              "num": 1, "expireTime": 0.0, "getTime": 0.0})
    _p, _o, name, _v = op_of(t.frames(strict=True)[0])
    check("ItemBag 的 name 是字符串 7", name == "7" and isinstance(name, str),
          name)
    t = _tx.Tx(st4.props)
    t.set(("story_task_bag", 1005), {"__tp__": "StoryTaskItem", "id": 1005,
                                     "state": 3, "get_time": 0,
                                     "start_time": 0})
    _p, _o, name, _v = op_of(t.frames(strict=True)[0])
    check("StoryTaskBag 的整数键在帧里是字符串 1005", name == "1005", name)
    # IntList：props 里写成 #3 也要能正确变成位置 3
    st4.props["char_list"]["#3"] = 9003
    t = _tx.Tx(st4.props)
    t.set(("char_list",), {"__tp__": "IntList",
                           "__id__": st4.props["char_list"]["__id__"],
                           "#1": 9001, "#2": 9002, "#3": 9004})
    _p, op, name, val = op_of(t.frames(strict=True)[0])
    check("IntList 整表替换 -> OP_CHANGE", op == _tx.OP_CHANGE, op)
    check("IntList 的 #N 键在帧里是整数键",
          isinstance(val, dict) and 1 in val and 3 in val, val)

    print("\n[5] _oo_* 不出现在帧里（顶层 + 嵌套）")
    st5 = state("txopt_d")
    st5.props["_oo_secret"] = "不该出现"
    t = _tx.Tx(st5.props)
    t.set(("street_info", 1011), {"__tp__": "StreetInfo", "_oo_hidden": 42,
                                  "prestige_level": 1})
    frames = t.frames(strict=True)
    blob = b"".join(bytes(f) for f in frames)
    check("帧里没有 _oo_hidden（嵌套层）", b"_oo_hidden" not in blob)
    check("帧里没有 _oo_secret（顶层）", b"_oo_secret" not in blob)
    _p, _o, _n, val = op_of(frames[0])
    check("嵌套 __tp__ 被剥掉（只保留根属性的）",
          isinstance(val, dict) and "__tp__" not in val, val)

    print("\n[6] 回归：发奖后客户端第 1 格不被覆盖")
    st6 = state("txopt_e")
    bag = st6.props["item_bag"]
    first = bag.get("1") or bag.get(1)
    check("初始第 1 格存在", isinstance(first, dict), first)
    first_item = first.get("itemId")
    pre = msmod._live_init_dict("txopt_e")
    got = story.add_item(st6.props, 12345, 3)
    check("add_item 返回新格子", bool(got), got)
    num, gkey, is_new = got
    check("新格子是新建的", is_new is True, is_new)
    check("新格子的键不是 1（不能覆盖第 1 格）", str(gkey) != "1", gkey)
    grid = st6.props["item_bag"][gkey]
    frames = [msmod.build_property_sync(
        "", st6.props["item_bag"]["__id__"], _tx.OP_ADD, str(gkey), grid)]
    _p, op, name, _v = op_of(frames[0])
    check("发奖帧用 OP_ADD", op == _tx.OP_ADD, op)
    check("发奖帧的 name 是新格子键", name == str(gkey), (name, gkey))
    _sc, errs = frames_safe(pre, frames, "发奖")
    check("发奖帧在客户端不会崩", not errs, errs[:2])
    after = _sc.props.get("item_bag")
    if hasattr(after, "props"):      # 自定义类型的根属性在影子里是 CustomObj
        after = after.props
    if isinstance(after, dict):
        g1 = after.get("1") or after.get(1)
        if hasattr(g1, "props"):
            g1 = g1.props
        check("客户端第 1 格仍是原道具",
              isinstance(g1, dict) and g1.get("itemId") == first_item,
              (g1.get("itemId") if isinstance(g1, dict) else g1, first_item))
    else:
        check("客户端 item_bag 存在", False, type(after))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
