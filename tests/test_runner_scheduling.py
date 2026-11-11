#!/usr/bin/env python3
"""守卫：测试调度器不许静默漏跑套件（2026-11-11）。

## 为什么需要

run_all_tests.py 改成并行之后，「少跑了一个套件」这种故障**不会体现在
bad 数组里** —— selfcheck 只看 suites / items / bad，少跑了套件
suites 变小、bad 仍是空，看起来还是绿的。

所以调度器必须自己对账（见 _report 的 expected 参数），
这个测试再守两层：

  [1] _suite_names() 找到的就是 tests/ 下全部 test_*.py，一个不少
  [2] _order() 只是重排序，不许增删（并行按它派活）
  [3] _report 收到缺项时会记成失败，而不是当没发生

## 破坏性验证

  _order() 里加一句 names[:-1]        -> [2] 红
  _report 去掉 expected 对账          -> [3] 红
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import run_all_tests as R  # noqa: E402

_P = []
_F = []


def check(name, cond, got=None):
    if cond:
        _P.append(name)
        print(f"  OK   {name}")
    else:
        _F.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def run():
    print("[1] _suite_names() 覆盖 tests/ 下全部 test_*.py")
    td = os.path.join(R.ROOT, "tests")
    on_disk = sorted(f[:-3] for f in os.listdir(td)
                     if f.startswith("test_") and f.endswith(".py"))
    got = R._suite_names()
    check(f"数量一致（{len(on_disk)} 个）", len(got) == len(on_disk),
          (len(got), len(on_disk)))
    check("★ 一个不少", set(got) == set(on_disk),
          sorted(set(on_disk) - set(got)))
    check("没有重复", len(got) == len(set(got)))
    check("数量合理（>50）", len(got) > 50, len(got))

    print()
    print("[2] ★ _order() 只重排序，不增删")
    ordered = R._order(got)
    check("长度不变", len(ordered) == len(got), (len(ordered), len(got)))
    check("集合不变（没漏也没多）", set(ordered) == set(got),
          sorted(set(got) ^ set(ordered)))
    check("无重复", len(ordered) == len(set(ordered)))

    print()
    print("[3] ★ _report 对账：少了套件要记失败，不能假绿")
    fake = [{"name": got[0], "ok": 5, "fail": 0, "rc": 0, "out": ""}]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_with = R._report(list(fake), False, expected=got)
        rc_without = R._report(list(fake), False)
    noisy = buf.getvalue()
    check("缺套件 -> 退出码非 0", rc_with != 0, rc_with)
    check("不传 expected -> 只看 bad（对照）", rc_without == 0, rc_without)
    check("★ 缺的套件被逐个点名并说明原因",
          got[-1] in noisy and "调度丢了套件" in noisy, len(noisy))

    print()
    print("[4] 结果解析：通过/失败 项数要能读出来")
    check("正常格式", R._parse("通过 12 项，失败 0 项") == (12, 0),
          R._parse("通过 12 项，失败 0 项"))
    check("有失败", R._parse("通过 3 项，失败 2 项") == (3, 2),
          R._parse("通过 3 项，失败 2 项"))
    check("没有汇总行 -> (0, -1)", R._parse("什么都没输出") == (0, -1),
          R._parse("什么都没输出"))

    print()
    print(f"通过 {len(_P)} 项，失败 {len(_F)} 项")
    if _F:
        print("失败项：" + ", ".join(_F))
    return 0 if not _F else 1


if __name__ == "__main__":
    sys.exit(run())
