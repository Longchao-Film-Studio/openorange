#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UP031 转换器 v2：ast 节点级改写 + ast.unparse 序列化（语法保证正确）。

只转换「格式串是常量、且只含 %s/%d/%f/%x/%X 基本转换码、源串无花括号、无映射、
无 %%」的 BinOp(Mod)。转换后 ast.parse(new_src) 必须通过。

用法: python tools/fix_up031_v2.py            # 干跑
      python tools/fix_up031_v2.py --apply   # 写入
"""
import ast, json, re, subprocess, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SIMPLE = {"s": "{}", "d": "{}", "f": "{:f}", "x": "{:x}", "X": "{:X}"}


class Rewrite(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        if not (isinstance(node.op, ast.Mod)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)):
            return node
        fmt = node.left.value
        if "{" in fmt or "}" in fmt or "%(" in fmt or "%%" in fmt:
            return node
        convs = re.findall(r"%([sdfxX])(?![0-9])", fmt)
        if not convs or len(convs) != fmt.count("%"):
            return node
        if not all(c in SIMPLE for c in convs):
            return node
        new_fmt = re.sub(r"%([sdfxX])(?![0-9])",
                         lambda m: SIMPLE[m.group(1)], fmt)
        node.left = ast.Constant(value=new_fmt)
        if isinstance(node.right, ast.Tuple):
            call = ast.Call(
                func=ast.Attribute(value=node.left, attr="format", ctx=ast.Load()),
                args=list(node.right.elts), keywords=[])
        else:
            call = ast.Call(
                func=ast.Attribute(value=node.left, attr="format", ctx=ast.Load()),
                args=[node.right], keywords=[])
        return ast.copy_location(call, node)


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
    total = done = files = 0
    for path, its in byfile.items():
        total += len(its)
        src = open(path, encoding="utf-8", errors="replace").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        old_fmt_n = src.count(".format(")
        new_tree = Rewrite().visit(tree)
        ast.fix_missing_locations(new_tree)
        new_src = ast.unparse(new_tree)
        try:
            ast.parse(new_src)
        except SyntaxError:
            continue
        gained = new_src.count(".format(") - old_fmt_n
        if gained <= 0:
            continue
        done += gained
        files += 1
        if apply_mode:
            open(path, "w", encoding="utf-8", newline="\n").write(new_src)
    print('ruff 总数 {} / 可转换 {} / 文件 {} / apply={}'.format(total, done, files, apply_mode))


if __name__ == "__main__":
    main()
