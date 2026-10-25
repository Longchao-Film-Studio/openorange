import ast
import json
import re
import subprocess
import sys

# % 转换码 -> .format 类型码（None 表示用空 spec）
TYPEMAP = {"s": "", "r": "!r", "d": "d", "i": "d", "o": "o", "x": "x", "X": "X",
           "e": "e", "E": "E", "f": "f", "F": "F", "g": "g", "G": "G"}

PAT = re.compile(r"%([-+ 0#]*)(\d+)?(?:\.(\d+))?([srdioxXeEfFgG%])")


def conv_fmt(fmt):
    """返回 (new_fmt, 占位符数) 或 None。"""
    out = []
    i = 0
    n = len(fmt)
    nph = 0
    while i < n:
        c = fmt[i]
        if c == "{":
            out.append("{{")
            i += 1
            continue
        if c == "}":
            out.append("}}")
            i += 1
            continue
        if c != "%":
            out.append(c)
            i += 1
            continue
        m = PAT.match(fmt, i)
        if not m:
            return None
        flags, width, prec, conv = m.group(1), m.group(2), m.group(3), m.group(4)
        i = m.end()
        if conv == "%":
            if flags or width or prec:
                return None
            out.append("%")
            continue
        if conv not in TYPEMAP:
            return None
        typ = TYPEMAP[conv]
        spec = ""
        if flags or width or prec:
            align = ""
            fill = ""
            if "-" in flags:
                align = "<"
            for ch in flags:
                if ch == "0":
                    fill = "0"
                elif ch == "+":
                    spec += "+"
                elif ch == " ":
                    spec += " "
                elif ch == "#":
                    spec += "#"
            if width:
                spec += width
            if prec:
                spec += "." + prec
            if typ.startswith("!"):
                head = typ
                tail = ""
                if align or fill or spec:
                    tail = ":" + (align if align else "") + (fill if fill and not align else "") + spec
                out.append("{" + head + tail + "}")
                nph += 1
                continue
            body = (align + fill if fill else align) + spec + typ
            out.append("{" + (":" + body if body else "") + "}")
            nph += 1
            continue
        if typ.startswith("!"):
            out.append("{" + typ + "}")
        elif typ:
            out.append("{:" + typ + "}")
        else:
            out.append("{}")
        nph += 1
    if nph == 0:
        return None
    return "".join(out), nph


def has_conv_desc(node):
    """node 内部是否还有可转换的 BinOp(Mod)。有则本轮跳过它（只转最内层）。"""
    for ch in ast.walk(node):
        if ch is node:
            continue
        if (isinstance(ch, ast.BinOp) and isinstance(ch.op, ast.Mod)
                and isinstance(ch.left, ast.Constant)
                and isinstance(ch.left.value, str)
                and conv_fmt(ch.left.value)):
            return True
    return False


def main():
    apply = "--apply" in sys.argv
    out = subprocess.run(
        ["python", "-m", "ruff", "check", ".", "--select", "UP031",
         "--output-format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    items = json.loads(out.stdout)
    byfile = {}
    for it in items:
        byfile.setdefault(it["filename"], []).append(it)
    total = done = files = skipped = 0
    for path in sorted(byfile):
        its = byfile[path]
        total += len(its)
        src = open(path, encoding="utf-8", errors="replace").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            skipped += len(its)
            continue
        lines = src.splitlines(keepends=True)
        offsets = [0]
        for ln in lines:
            offsets.append(offsets[-1] + len(ln))

        def abs_pos(lineno, col):
            line = lines[lineno - 1]
            head = line.encode("utf-8")[:col].decode("utf-8", "ignore")
            return offsets[lineno - 1] + len(head)

        edits = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)):
                continue
            left = node.left
            if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                continue
            r = conv_fmt(left.value)
            if not r:
                continue
            if has_conv_desc(node):
                continue
            new_fmt, nph = r
            right = node.right
            if isinstance(right, ast.Tuple):
                if any(isinstance(e, ast.Starred) for e in right.elts):
                    continue
                if len(right.elts) != nph:
                    continue
                parts = [ast.get_source_segment(src, e) for e in right.elts]
                if any(p is None for p in parts):
                    continue
                args_src = ", ".join(parts)
            else:
                if nph != 1:
                    continue
                args_src = ast.get_source_segment(src, right)
                if args_src is None:
                    continue
            new_expr = repr(new_fmt) + ".format(" + args_src + ")"
            try:
                ast.parse(new_expr)
            except SyntaxError:
                continue
            edits.append((abs_pos(node.lineno, node.col_offset),
                          abs_pos(node.end_lineno, node.end_col_offset),
                          new_expr))
        if not edits:
            skipped += len(its)
            continue
        edits.sort(key=lambda e: -e[0])
        new_src = src
        for s0, e0, txt in edits:
            new_src = new_src[:s0] + txt + new_src[e0:]
        try:
            ast.parse(new_src)
        except SyntaxError:
            skipped += len(its)
            continue
        done += len(edits)
        files += 1
        if apply:
            open(path, "w", encoding="utf-8", newline=chr(10)).write(new_src)
    print(f"ruff {total} / 转换 {done} / 文件 {files} / 跳过 {skipped} / apply={apply}")


if __name__ == "__main__":
    main()
