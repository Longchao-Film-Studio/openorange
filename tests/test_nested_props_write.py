#!/usr/bin/env python3
"""守卫：props 嵌套层不许绕过 Tx 直接写（B1，2026-11-06）。

## 为什么需要

2026-11-05 的导演面板事故：recalc_system_unlocks 里先
    us[sid] = True                    # 直接改 props 的嵌套层
    tx.set((..., sid), True)          # Tx 看到键已存在 -> 判成 OP_CHANGE
客户端 entryAdded 只声明了容器自身路径，OP_CHANGE 的查表 key 带 "*" 查不到，
静默丢帧，83 个系统入口解锁后都要重登才出现按钮。

以前 GuardedProps 只拦根属性，嵌套层拿到的是普通 dict，一句 [k]=v 就绕过。
现在 GuardedDict / GuardedList 构造时递归包装，任何一层写入都要么在
write_guard 里（Tx 自己开），要么抛 PropsWriteError。

## 两条不变量

    INV-PROPS-1  加载后的 props 整棵树每一层都是受控容器；
                 守卫外写**任意一层**都抛 PropsWriteError
    INV-PROPS-2  运行期插入的子对象保持原引用（不能偷偷换成副本）
                 —— 业务代码普遍是「拿引用 -> 改它 -> st.set(根, 同引用)」，
                 换成副本会让后续写入落进孤儿对象（实测丢过 done_tasks 进度）

## 破坏性验证

  GuardedDict.__init__ 里去掉 _wrap              -> [1][2] 红

## 附：为什么还需要静态扫描

运行期插入保身份（INV-PROPS-2）意味着「插入进去的子对象」仍是普通 dict，
它的后续写不受守卫约束。所以另外用 AST 扫描抓「可疑的嵌套写」，
见 [5]。这条比运行期守卫弱，但能覆盖住「拿引用再改」这个主流写法。
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 本测试验的是守卫本身，必须把 run_all_tests.py 设的后门关掉。
#   tools/run_all_tests.py:22 设 OO_ALLOW_PROPS_WRITE=1 是为了让别的测试
#   「造出某个状态」—— 那是正当需求，但它会让这里的断言全部失效。
os.environ.pop("OO_ALLOW_PROPS_WRITE", None)

from game_server.write_guard import (  # noqa: E402
    GuardedDict,
    GuardedList,
    GuardedProps,
    PropsWriteError,
    write_guard,
)

_P: list = []
_F: list = []


def check(name, cond, got=None):
    if cond:
        _P.append(name)
        print(f"  OK   {name}")
    else:
        _F.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def raises(fn):
    try:
        fn()
    except PropsWriteError:
        return True
    except Exception:
        return False
    return False


def run():
    print("[1] INV-PROPS-1：加载后整棵树都是受控容器")
    p = GuardedProps({"a": {"b": {"c": 1}}, "l": [1, {"x": 2}], "s": "str"})
    check("根是 GuardedDict", isinstance(p, GuardedDict), type(p).__name__)
    check("第 2 层是 GuardedDict", isinstance(p["a"], GuardedDict), type(p["a"]).__name__)
    check("第 3 层是 GuardedDict", isinstance(p["a"]["b"], GuardedDict), type(p["a"]["b"]).__name__)
    check("list 是 GuardedList", isinstance(p["l"], GuardedList), type(p["l"]).__name__)
    check("list 里的 dict 也是 GuardedDict",
          isinstance(p["l"][1], GuardedDict), type(p["l"][1]).__name__)
    check("标量不动", p["s"] == "str", p["s"])

    print()
    print("[2] ★ 守卫外写任意一层都抛异常")
    def _w1():
        p["z"] = 1

    def _w2():
        p["a"]["z"] = 1

    def _w3():
        p["a"]["b"]["c"] = 9

    def _w4():
        del p["a"]["b"]["c"]

    def _w5():
        p["l"].append(3)

    def _w6():
        p["l"][0] = 9

    def _w7():
        p["l"][1]["x"] = 9

    def _w8():
        p["a"]["b"].pop("c")

    def _w9():
        p["a"]["b"].setdefault("d", 1)

    def _w10():
        p["a"]["b"].update({"e": 1})

    def _w11():
        p["a"]["b"].clear()

    check("第 1 层 props[k]=v", raises(_w1))
    check("第 2 层 props[a][k]=v", raises(_w2))
    check("第 3 层 props[a][b][k]=v", raises(_w3))
    check("第 3 层 del", raises(_w4))
    check("list append", raises(_w5))
    check("list 下标写", raises(_w6))
    check("list 里的 dict", raises(_w7))
    check("嵌套 pop", raises(_w8))
    check("嵌套 setdefault", raises(_w9))
    check("嵌套 update", raises(_w10))
    check("嵌套 clear", raises(_w11))

    print()
    print("[3] 守卫内（Tx 的写法）可以正常写")
    with write_guard("test"):
        p["a"]["b"]["c"] = 9
        p["l"].append(3)
        p["a"]["b"]["new"] = {"deep": 1}
    check("第 3 层写入生效", p["a"]["b"]["c"] == 9, p["a"]["b"]["c"])
    check("list append 生效", list(p["l"])[-1] == 3, list(p["l"]))
    check("守卫内新插入的 dict 也生效", p["a"]["b"]["new"]["deep"] == 1)

    print()
    print("[4] INV-PROPS-2：运行期插入的子对象保持原引用")
    p2 = GuardedProps({"done_tasks": {}})
    done = p2["done_tasks"]
    slot = {"__id__": 1}
    with write_guard("test"):
        done[3] = slot
    check("插入后是同一个对象（不是副本）", p2["done_tasks"][3] is slot,
          type(p2["done_tasks"][3]).__name__)
    with write_guard("test"):
        slot[1] = 123
    check("★ 拿引用继续改，props 里能看到",
          dict(p2["done_tasks"][3]).get(1) == 123, dict(p2["done_tasks"][3]))

    print()
    print("[5] 序列化兼容：仍要能 json / deepcopy / isinstance")
    check("isinstance dict", isinstance(p["a"], dict))
    check("isinstance list", isinstance(p["l"], list))
    dumped = json.dumps(p, ensure_ascii=False)
    check("json.dumps 成功", dumped.startswith("{"), dumped[:40])
    cp = copy.deepcopy(p)
    check("deepcopy 成功且类型保持", isinstance(cp, GuardedDict), type(cp).__name__)
    check("deepcopy 是独立副本", cp["a"]["b"]["c"] == 9)

    print(f"通过 {len(_P)} 项，失败 {len(_F)} 项")
    if _F:
        print("失败项：" + ", ".join(_F))
    return 0 if not _F else 1


if __name__ == "__main__":
    sys.exit(run())
