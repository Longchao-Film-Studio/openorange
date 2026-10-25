#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UP031 转换器 v3：行级替换 —— 只把被转换语句的行换成 unparse 文本，其余不动。

diff 最小化：非目标行（含排版、注释、空行）原样保留。
"""
import ast, json, re, subprocess, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SIMPLE = {"s": "{}", "d": "{}", "f": "{:f}", "x": "{:x}", "X": "{:X}"}


def convertible(node):
    if not (isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)):
        return False
    fmt = node.left.value
    if "{" in fmt or "}" in fmt or "%(" in fmt or "%%" in fmt:
        return False
    convs = re.findall(r"%([sdfxX])(?![0-9])", fmt)
    return bool(convs) and len(convs) == fmt.count("%") and all(c in SIMPLE for c in convs)


def main():
    apply_mode = "--apply" in sys.argv
    out = subprocess.run(
        ["python", "-m", "ruff", "check", ".", "--select", "UP031",
         "--output-format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    items = json.loads(out.stdout)
    byfile = {}
    for it in items:
        byfile.setdefault(it["filename"], []).append(it)
    total = done = skipped = files = 0
    for path, its in byfile.items():
        total += len(its)
        src = open(path, encoding="utf-8", errors="replace").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            skipped += len(its)
            continue
        lines = src.splitlines(keepends=True)
        # 收集本文件内可转换的顶层/嵌套语句（按其所在行范围整语句替换）
        edits = []   # (lo, hi, new_text)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and convertible(node):
                # 找到包含它的**最内层完整语句**：用 parent 不便，直接用 BinOp 自己的
                # 行范围 + 借 unparse 验证
                lo, hi = node.lineno, node.end_lineno
                stmt_text = ast.unparse(node)
                if "format(" in stmt_text:
                    edits.append((lo, hi, stmt_text))
        # 去重（同一行范围只留一个）
        edits = sorted(set(edits), key=lambda e: -e[0])
        changed = 0
        for lo, hi, text in edits:
            # 缩进取原块首行
            indent = re.match(r"[ \t]*", lines[lo - 1]).group(0)
            body = "\n".join(indent + t for t in text.splitlines())
            lines[lo - 1:hi] = [body + "\n"]
            changed += 1
        if not changed:
            skipped += len(its)
            continue
        new_src = "".join(lines)
        try:
            ast.parse(new_src)
        except SyntaxError as _se:
            if not apply_mode:
                print("WHOLE-FAIL:", path.split(chr(92))[-1], _se)
            continue
        done += changed
        files += 1
        if apply_mode:
            open(path, "w", encoding="utf-8", newline="\n").write(new_src)
    print('ruff 总数 {} / 语句转换 {} / 文件 {} / 跳过 {} / apply={}'.format(total, done, files, skipped, apply_mode))


if __name__ == "__main__":
    main()
