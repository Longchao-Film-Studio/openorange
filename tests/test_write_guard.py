#!/usr/bin/env python
"""属性写入守卫：绕过 Tx直接写 props必须报错。

##这个测试要防的是什么

实机 bug（点「领取」经验不涨）的根因是改了属性却没发同步帧。
收敛完手工拼帧之后，还需要让「绕过 Tx直接写属性」在代码层面失败，
否则下次有人图省事直接 props下标赋值，同样的 bug会再出现一次。

##为什么测试环境要放行

测试要「造出某个状态」再验证行为，造状态本来就该能直接写属性。
所以 run_all_tests会设 OO_ALLOW_PROPS_WRITE=1。

本测试做的事情就是临时关掉那个开关，证明守卫真的在拦。
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#与 tools/run_all_tests.py的汇总正则对齐：数字两侧有空格。
SUMMARY_LINE = "通过 {}项，失败 {}项"
_ok = 0
_bad: list[str] = []


def check(name, cond, detail=""):
    global _ok
    if cond:
        _ok += 1
        print(f" PASS {name}")
    else:
        _bad.append(name)
        print(f" FAIL {name}" + (f" | {detail}" if detail else ""))


def run():
    from game_server.write_guard import (
        GuardedProps,
        PropsWriteError,
        in_guard,
        write_guard,
    )

    #临时关掉测试放行开关，让守卫回到生产行为
    os.environ.pop("OO_ALLOW_PROPS_WRITE", None)

    print("\n[1]守卫外直接写根属性 ->必须报错")
    props = GuardedProps({"level":1})
    try:
        props["level"] =2
        check("直接写被拒绝", False, "居然成功了")
    except PropsWriteError as e:
        check("直接写被拒绝", True)
        check("报错信息指明了走 Tx", "Tx" in str(e), str(e)[:60])
        check("值没被改掉", props["level"] ==1, props["level"])

    print("\n[2]守卫内允许写")
    with write_guard("test"):
        props["level"] =3
    check("守卫内写入生效", props["level"] ==3, props["level"])
    check("退出后守卫关闭", not in_guard())

    print("\n[3]嵌套守卫：内层退出不影响外层")
    with write_guard("outer"):
        with write_guard("inner"):
            props["level"] =4
        check("内层退出后仍在外层守卫里", in_guard())
        props["level"] =5
    check("嵌套写入都生效", props["level"] ==5, props["level"])

    print("\n[4]私有键 _oo_不受限制：客户端根本不知道它们存在")
    props["_oo_cur_copy"] =77
    check("_oo_键可直写", props["_oo_cur_copy"] ==77)
    props.pop("_oo_cur_copy")
    check("_oo_键可直 pop", "_oo_cur_copy" not in props)

    print("\n[5]删除/批量操作同样受限")
    for name, fn in (
        ("del", lambda: props.__delitem__("level")),
        ("pop", lambda: props.pop("level")),
        ("setdefault", lambda: props.setdefault("level",0)),
        ("update", lambda: props.update({"level":9})),
        ("clear", props.clear),
    ):
        try:
            fn()
            check(f"{name}被拒绝", False, "居然成功了")
        except PropsWriteError:
            check(f"{name}被拒绝", True)

    print("\n[6] Tx内部写属性不报错（Tx自己开守卫）")
    import copy

    from game_server.ms.initdict import AVATAR_INIT_DICT
    from game_server.property.tx import Tx

    p2 = GuardedProps(copy.deepcopy(AVATAR_INIT_DICT["__Properties__"]))
    tx = Tx(p2)
    tx.set(("level",),9)
    check("Tx.set能写", p2["level"] ==9, p2["level"])
    check("Tx产生了帧", len(tx.frames()) ==1, len(tx.frames()))
    check("Tx退出后守卫关闭", not in_guard())

    print("\n[7]结构性不变量：写 props的文件都必须 import write_guard")
    import re
    from pathlib import Path

    gs = Path(ROOT) / "game_server"
    offenders = []
    for f in sorted(gs.rglob("*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        if "write_guard" in src:
            continue
        for i, line in enumerate(src.splitlines(),1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not re.search(r"\bst\.props\[[^\]]+\]\s*=", stripped):
                continue
            if '"_oo_' in stripped:
                continue
            offenders.append(f"{f.relative_to(ROOT)}:{i} {stripped[:50]}")
    check("写 props的文件都 import了 write_guard", not offenders, offenders[:4])

    print()
    print(f"===结果: PASS={_ok} FAIL={len(_bad)} ===")
    if _bad:
        print("失败项：" + "、".join(_bad))
        print(SUMMARY_LINE.format(_ok, len(_bad)))
        return 1
    print(SUMMARY_LINE.format(_ok,0))
    return 0

if __name__ == "__main__":
    sys.exit(run())
