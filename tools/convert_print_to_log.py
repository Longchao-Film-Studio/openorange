#!/usr/bin/env python3
_QUOTED = "^(f|r|b)?(['\"])"
"""print -> logging AST 转换器（一次性迁移工具）。

用法:
    python tools/convert_print_to_log.py --dry   # 预览改动清单
    python tools/convert_print_to_log.py --write # 写入

规则:
  print(X, flush=True)                -> _log.info(X)
  print("fmt" % x, flush=True)        -> _log.info("fmt", x)      # logging 延迟格式化
  print(a, b, c, flush=True)          -> _log.info("%s %s %s", a, b, c)
  print("...error/失败/拒绝...")       -> _log.warning(...)
分级按消息文本里的现有标签:
  [GATE]/[ms]/[game]/hexdump 类大流量  -> debug
  error/失败/拒绝/异常/警告/危险        -> warning
  其余                                 -> info
每文件头部自动注入: _log = get_logger(__name__)（缺 import 时一并补）。
范围: game_server/ shared/ web/ 的生产代码；tests/tools 不动。
"""
import argparse
import ast
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WARN_PAT = re.compile(r"error|失败|拒绝|异常|警告|危险|invalid|fail|拒绝|不足|无法", re.IGNORECASE)
DEBUG_PAT = re.compile(r"^\[(GATE|ms|game)\]|RX|TX", re.IGNORECASE)

LOG_FOOTER = "_log = get_logger(__name__)"


def level_of(src):
    if WARN_PAT.search(src):
        return "warning"
    if DEBUG_PAT.search(src):
        return "debug"
    return "info"


def top_level_args(call):
    """拆 print 的顶层参数文本列表（按逗号，括号/字符串感知）。"""
    args = []
    depth = 0
    q = None
    cur = []
    i = 0
    src = call
    while i < len(src):
        ch = src[i]
        if q:
            cur.append(ch)
            if ch == "\\":
                cur.append(src[i + 1] if i + 1 < len(src) else "")
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    # 尾部 kwargs 剥离（flush=True / file=...）
    if "=" in tail:
        m = re.match(r"^(flush|file|sep|end)\s*=", tail)
        if m:
            tail = ""
        else:
            # 形如  x, flush=True —— 剥最后一个 kwargs
            parts = re.split(r",\s*(flush|file|sep|end)\s*=", tail)
            tail = parts[0].strip()
    if tail:
        args.append(tail)
    return [a for a in args if a]


def convert_call(call_src):
    """print(ARGS) 的 ARGS -> (level, new_args_text)。"""
    args = top_level_args(call_src)
    if not args:
        return "info", '""'
    lvl = level_of(args[0])
    if len(args) == 1:
        return lvl, args[0]
    first = args[0]
    # 首参是"纯字符串字面量"（含 % 格式或拼接）-> logging 延迟格式化
    if re.match(_QUOTED, first) and not first.startswith('f') and not first.startswith('f"'):
        # 字符串 + 其余参数：保持 % 语义 —— logging 用 args 传
        rest = args[1:]
        if len(rest) == 1:
            return lvl, f"{first}, {rest[0]}"
        return lvl, "{}, ({},)".format(first, ", ".join(rest))
    # 多参 print -> "%s %s" 形式
    n = len(args)
    return lvl, '"{}" % ({},)'.format(" ".join(["%s"] * n), ", ".join(args))


def process_file(path, write):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    crlf = "\r\n" in src
    if crlf:
        src = src.replace("\r\n", "\n")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None

    # 收集 print 调用（模块级函数调用，非 obj.print）
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            calls.append(node)

    if not calls:
        return None

    # 从后往前替换（行号不失效）
    lines = src.split("\n")
    levels = {}
    changed = 0
    for node in sorted(calls, key=lambda n: (-n.lineno, -n.col_offset)):
        # 单行 print 才转（跨行 print 保守跳过——本仓库极少）
        end_line = getattr(node, "end_lineno", node.lineno)
        if end_line != node.lineno:
            continue
        line = lines[node.lineno - 1]
        col = node.col_offset
        # 提取 print(...) 的完整括号段
        start = line.index("(", col)
        depth = 0
        q = None
        end = None
        for i in range(start, len(line)):
            ch = line[i]
            if q:
                if ch == "\\":
                    i += 1
                    continue
                if ch == q:
                    q = None
                continue
            if ch in "\"'":
                q = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        inner = line[start + 1:end]
        lvl, new_args = convert_call(inner)
        prefix = line[:col]
        suffix = line[end + 1:]
        levels[lvl] = levels.get(lvl, 0) + 1
        new_line = f"{prefix}_log.{lvl}({new_args}){suffix}"
        lines[node.lineno - 1] = new_line
        changed += 1

    if not changed:
        return None

    # 注入 import + _log 赋值。
    # 插入点：最后一个“顶层” from/import 之后（跳过跨行括号的 import ——
    #   web/app.py 的 from web.assets import ( 跨 8 行，插中间会语法炸）。
    if changed:
        last_top = -1
        depth = 0
        for i, l in enumerate(lines):
            if depth == 0 and re.match(r"^(from |import )", l):
                last_top = i
            depth += l.count("(") + l.count("[") - l.count(")") - l.count("]")
            depth = max(depth, 0)
        last_top = max(last_top, 0)
        rel_norm = os.path.relpath(path, ROOT).replace("\\", "/")
        pkg_depth = rel_norm.count("/")          # 距仓库根的包层级
        dots = "." * pkg_depth
        log_import = f"from {dots}log_tee import get_logger"
        lines.insert(last_top + 1, "")
        lines.insert(last_top + 2, log_import)
        lines.insert(last_top + 3, LOG_FOOTER)
    if changed:
        text = "\n".join(lines)
    if write:
        with open(path, "w", encoding="utf-8", newline=("\r\n" if crlf else "\n")) as f:
            f.write(text)
    return {"file": os.path.relpath(path, ROOT), "changed": changed, "levels": levels}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    targets = []
    for sub in ("game_server", "shared", "web"):
        d = os.path.join(ROOT, sub)
        for dirpath, _dirnames, filenames in os.walk(d):
            if "__pycache__" in dirpath:
                continue
            for fn in filenames:
                if fn.endswith(".py"):
                    targets.append(os.path.join(dirpath, fn))

    total_files = total_calls = 0
    lvl_sum = {}
    for p in sorted(targets):
        r = process_file(p, args.write)
        if r:
            total_files += 1
            total_calls += r["changed"]
            for k, v in r["levels"].items():
                lvl_sum[k] = lvl_sum.get(k, 0) + v
            print('{:<46} {:3d} 处  {}'.format(r["file"], r["changed"], r["levels"]))
    print(f'\n文件 {total_files}，print {total_calls} 处，分级 {lvl_sum}')
    if not args.write:
        print("(dry run —— 加 --write 落盘)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
