#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UP031 simple 类自动转换器：只处理「单行、仅 %s/%d/%f/%x/%X、无映射无对齐无 %%」的
格式化，转换后用 ast.parse 验证语法。任何不确定都跳过。

用法: python tools/fix_up031_simple.py          # 干跑
      python tools/fix_up031_simple.py --apply # 写入
"""
import ast, json, os, re, subprocess, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 只支持这些转换：基本转换码，无宽度无精度（有宽度的跳过）
SIMPLE = {"s": "{}", "d": "{}", "f": "{:f}", "x": "{:x}", "X": "{:X}"}


def convert_fmt(literal):
    """把 % 格式串字面量内容转为 .format 内容。不支持就返回 None。"""
    out = []
    i = 0
    n = len(literal)
    while i < n:
        c = literal[i]
        if c in "{}":
            return None                       # 源串里已有花括号，跳过（避免双写判断出错）
        if c != "%":
            out.append(c)
            i += 1
            continue
        m = re.match(r"%([sdfxX])", literal[i:])
        if not m:
            return None                       # %% / %(x)s / 宽度精度 / %r 等一律跳过
        out.append(SIMPLE[m.group(1)])
        i += 1 + m.end()
    return "".join(out)


skip_reason = {"nomatch": 0, "tail": 0, "fmt": 0, "re": 0, "syntax": 0}


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
    total = done = skipped = 0
    for path, its in byfile.items():
        src = open(path, encoding="utf-8", errors="replace").read()
        lines = src.splitlines(keepends=True)
        changed = False
        # 从后往前按行改（每处都在单行内）
        for it in sorted(its, key=lambda x: -x["location"]["row"]):
            total += 1
            row = it["location"]["row"]
            line = lines[row - 1]
            # 定位 "....%s..." % expr 形态（expr 到行尾）。引号用 chr 构造避免转义地狱
            m = re.search(
                "(" + chr(39) + '|' + chr(34) + ")"
                + "([^" + chr(39) + chr(34) + "]*%[sdfxX][^" + chr(39) + chr(34) + "]*)"
                + "(" + chr(39) + '|' + chr(34) + ")"
                + "(\\s*)%\\s*(\\(.*\\)|[^\\s%][^%]*)"
                + "(.*)$", line)
            if not m:
                if not apply_mode: skip_reason["nomatch"] += 1
                skipped += 1
                continue
            g = m.groups()
            if not apply_mode and total <= 3:
                print("DBG:", [repr(x) for x in m.groups()])
            q, fmt, sp, args, tail = g[0], g[1], g[2], g[3], g[4]
            # g[5] 是行尾组，忽略
            new_fmt = convert_fmt(fmt)
            if new_fmt is None:
                if not apply_mode: skip_reason["fmt"] += 1
                skipped += 1
                continue
            if not apply_mode: skip_reason["re"] += 1
            # args 若是 (a, b) 单元组形态保持；.format 直接收原参数列表
            args2 = args
            if args2.startswith("(") and args2.endswith(")"):
                args2 = args2[1:-1].strip()      # (x) -> x
            new_line = line[:m.start()] + q + new_fmt + q + sp + ".format(" + args2 + ")" + tail
            # 语法验证
            try:
                ast.parse(new_line)
            except SyntaxError as _se:
                if not apply_mode: skip_reason["syntax"] += 1
                if not apply_mode and skip_reason["syntax"] < 3:
                    print("SYNTAX-FAIL:", repr(new_line[:170]), repr(str(_se)))
                skipped += 1
                continue
            lines[row - 1] = new_line
            changed = True
            done += 1
        if changed and apply_mode:
            open(path, "w", encoding="utf-8", newline="").write("".join(lines))
    print('总数 {} / 转换 {} / 跳过 {} / 原因 {} / apply={}'.format(total, done, skipped, skip_reason, apply_mode))


if __name__ == "__main__":
    main()
