#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""元测试：禁止「空断言」—— check(<名字>, True/False) 字面量。

★ 为什么必须有（2026-10-22 检讨）
  功能解锁 73% 失效一直没被发现，原因之一是测试里混着空断言：
    tests/test_story.py:125  check("1005 完成后 头衔(2)/龙胤(4)/成就(55) 应解锁", True)
  条件写死 True，永远不会红。我把「1573 项全绿」当成了「系统完整」的证据。

★ 什么算合法
  try/except 里 except 分支的 `check(..., False, err)` 是合法的 —— 它表示
  「上面那段不该抛异常」，真正成功时走 try 末尾的 `check(..., True)`。
  所以**只在 except 块里**的字面量豁免。

用法: python tests/test_no_vacuous_assert.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 豁免清单：(相对路径, 行号) -> 理由。每条必须写清「为什么这不是空断言」。
# 豁免清单：(相对路径, 行号) -> 理由。每条必须写清「为什么这不是空断言」。
# ★ 只查 `True` 字面量：`check(..., False, reason)` 是「守卫分支主动标失败」，
#   本身永远不会「假通过」，不在此列。
ALLOW = {
    ("tests/test_position.py", 159):
        "可达性断言：上一行 guarded_handle 抛异常会直接崩，能走到这行就说明没崩",
    ("tests/test_shadow_coverage.py", 149):
        "在 try 内、check_frames 调用之后；能走到就说明没抛 AssertionError",
    ("tests/test_story.py", 95):
        "信息性：1000 无奖励是已知事实，有奖励时的真实验证在 if 分支里",
    ("tests/test_story_sync_contract.py", 284):
        "文档化不适用：首通本来就不走 changed，真验证在第二次触发那一项",
    ("tests/test_story_sync_contract.py", 319):
        "文档化不适用：SECOND_PASS 里列出的是本次场景不可达的契约行",
    ("tests/test_phase2_coverage.py", 103):
        "汇总信息性输出；每条 RPC 的覆盖在上方逐条断言",
    ("tests/test_id_allocator.py", 130):
        "try 内可达性：check_frames 抛 AssertionError 会走 except 分支标失败",
    ("tests/test_ms_frames.py", 48):
        "try 内可达性：上面调用抛异常会走 except 分支标失败",
    ("tests/test_multi_account.py", 190):
        "try 内可达性：sync_frame 抛异常会走 except 分支标失败",
}

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def _py_files():
    out = []
    for sub in ("tests", "tools", "game_server"):
        base = os.path.join(ROOT, sub)
        for dirpath, _d, files in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _parent_map(tree):
    pm = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            pm[child] = node
    return pm


def _in_except(node, pm):
    p = node
    while p in pm:
        p = pm[p]
        if isinstance(p, ast.ExceptHandler):
            return True
    return False


def _is_check_call(node):
    """node 是不是 check(...) 调用。"""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    return isinstance(fn, ast.Name) and fn.id == "check"


def _literal_bool(node, idx):
    """取第 idx 个参数的布尔字面量；不是字面量返回 None。"""
    if len(node.args) <= idx:
        return None
    a = node.args[idx]
    if not isinstance(a, ast.Constant):
        return None
    if a.value is True or a.value is False:
        return a.value
    return None


def scan():
    """返回 [(相对路径, 行号, 说明)]。"""
    bad = []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except SyntaxError:
            continue
        pm = _parent_map(tree)
        for node in ast.walk(tree):
            if not _is_check_call(node):
                continue
            v = _literal_bool(node, 1)
            if v is not True:
                continue        # 只查 True：False 是「主动标失败」，不会假通过
            if v is None:
                continue
            if _in_except(node, pm):
                continue
            key = (rel, node.lineno)
            if key in ALLOW:
                continue
            bad.append((rel, node.lineno, "cond = %r" % v))
    return bad


def _live_check_lines():
    live = set()
    for path in _py_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8",
                             errors="replace").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if _is_check_call(node):
                live.add((rel, node.lineno))
    return live


def _count_literal(src, in_except):
    """在给定源码里数 check 的布尔字面量（可限定是否在 except 块内）。"""
    tree = ast.parse(src)
    pm = _parent_map(tree)
    n = 0
    for node in ast.walk(tree):
        if not _is_check_call(node):
            continue
        if _literal_bool(node, 1) is None:
            continue
        if _in_except(node, pm) != in_except:
            continue
        n += 1
    return n


def run():
    del passed[:]
    del failed[:]

    print("\n[1] 扫描全仓 check(<字面量>, True/False)")
    bad = scan()
    for rel, line, expr in bad:
        print("       %s:%s  %s" % (rel, line, expr))
    check("★ 没有空断言", not bad, len(bad))

    print("\n[2] 豁免清单不许有死条目")
    live = _live_check_lines()
    dead = [k for k in ALLOW if k not in live]
    check("★ 豁免清单没有死条目", not dead, dead)

    print("\n[3] 元测试自身有效")
    src1 = "def f():\n    check(\"x\", True)\n"
    check("★ 扫描器能抓到构造的空断言", _count_literal(src1, False) == 1,
          _count_literal(src1, False))
    src2 = ("def f():\n"
            "    try:\n"
            "        x = 1\n"
            "    except Exception as e:\n"
            "        check(\"y\", False, e)\n")
    check("★ except 块里的字面量被正确豁免", _count_literal(src2, True) == 1,
          _count_literal(src2, True))
    check("★ 且它不会被当成违规", _count_literal(src2, False) == 0,
          _count_literal(src2, False))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())