import ast, json, re, subprocess, sys

SIMPLE = {"s": "{}", "d": "{}", "f": "{:f}", "F": "{:F}",
          "x": "{:x}", "X": "{:X}", "e": "{:e}", "g": "{:g}"}


def conv_fmt(fmt):
    out = []
    i = 0
    n = len(fmt)
    nph = 0
    while i < n:
        c = fmt[i]
        if c in "{}":
            return None
        if c != "%":
            out.append(c)
            i += 1
            continue
        m = re.match(r"%([sdfFxXeg])", fmt[i:])
        if not m:
            return None
        out.append(SIMPLE[m.group(1)])
        nph += 1
        i += m.end()
    if nph == 0:
        return None
    return "".join(out), nph


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
    print("ruff %d / 转换 %d / 文件 %d / 跳过 %d / apply=%s"
          % (total, done, files, skipped, apply))


if __name__ == "__main__":
    main()
