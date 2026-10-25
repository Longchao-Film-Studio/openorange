#!/usr/bin/env python3
"""print -> logging AST 转换器（v2：支持跨行调用）。

用法:
    python tools/convert_print_to_log.py            # 预览（按级别列出每条）
    python tools/convert_print_to_log.py --write    # 落盘

规则:
  print(X, flush=True)          -> _log.info(X)
  print(X, file=sys.stderr)     -> _log.warning(X)
  print(a, b, c)                -> _log.info("%s %s %s", a, b, c)
  print(..., flush=True) 跨多行 -> 同上（整段替换成单行）

分级（按消息文本判断，逐条打印出来供人工复核）:
  debug    : maybe_hexdump/hexdump、逐帧/逐包诊断（RX/TX/帧类型/实体消息）
  warning  : 警告 / 拒绝 / 失败 / 异常 / 解析失败 / 错误 / error / 超时
  info     : 其余（启动、播种、状态变更、里程碑）

范围: game_server/ shared/ web/ 生产代码；tests/tools 不动。
幂等: 已经没有 print 的文件直接跳过。
"""

import argparse
import ast
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FOOTER = "_log = get_logger(__name__)"

WARN_PAT = re.compile(r"警告|拒绝|失败|异常|解析失败|错误|error|Error|FATAL|超时|不足|无法|忽略")
DEBUG_PAT = re.compile(r"maybe_hexdump|hexdump|帧类型|实体消息|RX |TX \{|TX \+")
# 绑定链里程碑（每条连接一次），不算逐帧噪音
MILESTONE_PAT = re.compile(
    r"ConnectServerResp|createChannelEntity|绑定账号|关联账号|活过 4 秒|reqType=")


def level_of(text, is_stderr):
    if is_stderr:
        return "warning"
    if MILESTONE_PAT.search(text):
        return "info"
    # ★ 失败优先于「逐帧」：'帧类型=... protobuf 解析失败' 属于告警，不是噪音
    if WARN_PAT.search(text):
        return "warning"
    if DEBUG_PAT.search(text):
        return "debug"
    return "info"


def convert_one(src, node):
    """返回 (新代码文本, 级别, 消息摘要)；不转换返回 None。"""
    args = [ast.get_source_segment(src, a) for a in node.args]
    kwargs = {}
    for kw in node.keywords:
        seg = ast.get_source_segment(src, kw.value)
        kwargs[kw.arg] = seg
    is_stderr = bool(kwargs.get("file") and "stderr" in kwargs["file"])

    joined = " ".join(args) if args else '""'
    lvl = level_of(joined, is_stderr)
    if not args:
        return (f'_log.{lvl}("")', lvl, '""')
    if len(args) == 1:
        return (f"_log.{lvl}({args[0]})", lvl, joined[:60])
    # 多参 print：转 logging 延迟格式化
    fmt = " ".join(["%s"] * len(args))
    return (f'_log.{lvl}("{fmt}", {", ".join(args)})', lvl, joined[:60])


def process(path, write, report):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    src = raw.replace("\r\n", "\n")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
    if not calls:
        return None

    lines = src.split("\n")
    changed = 0
    levels = {}
    # 从后往前替换：位置不失效
    for node in sorted(calls, key=lambda n: (-n.lineno, -n.col_offset)):
        res = convert_one(src, node)
        if res is None:
            continue
        new_text, lvl, summary = res
        sl, sc = node.lineno - 1, node.col_offset
        el, ec = node.end_lineno - 1, node.end_col_offset
        prefix = lines[sl][:sc]
        suffix = lines[el][ec:]
        lines[sl] = prefix + new_text + suffix
        if el > sl:
            del lines[sl + 1: el + 1]
        levels[lvl] = levels.get(lvl, 0) + 1
        report.append((os.path.relpath(path, ROOT), node.lineno, lvl, summary))
        changed += 1

    if not changed:
        return None

    # 注入 import + _log（插在最后一个顶层 import 之后；跳过跨行括号的 import）
    last_top, depth, in_import = -1, 0, False
    for i, l in enumerate(lines):
        if depth == 0 and re.match(r"^(from |import )", l):
            last_top = i
            in_import = True
        depth += l.count("(") + l.count("[") - l.count(")") - l.count("]")
        depth = max(depth, 0)
        # ★ 跨行 import（from x import (... ）的收尾行才是插入点：
        #   只认「起行」会把 import 插进括号里（tasks.py 就是这么炸的）
        if in_import and depth == 0:
            last_top = i
            in_import = False
    last_top = max(last_top, 0)
    if "from shared.log_tee import get_logger" not in "\n".join(lines):
        lines.insert(last_top + 1, "")
        lines.insert(last_top + 2, "from shared.log_tee import get_logger")
        lines.insert(last_top + 3, "")
        lines.insert(last_top + 4, LOG_FOOTER)

    text = "\n".join(lines)
    try:
        compile(text, str(path), "exec")
    except SyntaxError as e:
        return {"file": os.path.relpath(path, ROOT), "error": f"{e} @{e.lineno}"}

    if write:
        with open(path, "w", encoding="utf-8", newline=("\r\n" if crlf else "\n")) as f:
            f.write(text)
    return {"file": os.path.relpath(path, ROOT), "changed": changed, "levels": levels}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    targets = []
    for sub in ("game_server", "shared", "web"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, sub)):
            if "__pycache__" in dirpath:
                continue
            targets.extend(os.path.join(dirpath, f) for f in files if f.endswith(".py"))

    report = []
    total_files = total_calls = 0
    lvl_sum = {}
    for p in sorted(targets):
        r = process(p, args.write, report)
        if not r:
            continue
        if "error" in r:
            print("!! 语法校验失败，跳过:", r)
            continue
        total_files += 1
        total_calls += r["changed"]
        for k, v in r["levels"].items():
            lvl_sum[k] = lvl_sum.get(k, 0) + v

    if args.verbose:
        for f, ln, lvl, msg in report:
            print(f"{lvl:<7} {f}:{ln}  {msg}")
    print(f"\n文件 {total_files}，print {total_calls} 处，分级 {lvl_sum}")
    if not args.write:
        print("(预览 —— 加 --write 落盘)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
