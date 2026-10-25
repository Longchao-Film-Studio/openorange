#!/usr/bin/env python3
"""找出 Python 文件里「导入了但没用」的名字（P0 遗留清理用的小工具）。

★ 两种口径：
  默认      跳过带 # noqa 的行（项目约定：noqa 表示「这个导入是有意保留的」）
  --strict  连 # noqa 的行也查（用来核对「0 处」是不是真的 0）

用法:
    python -u tools/find_unused_imports.py <文件或目录> [...]

规则（故意保守，避免误报）：
  * 只看模块顶层的 import / from ... import
  * 名字在文件其余任何地方出现（含字符串里的 docstring 引用）就算用过
  * 带 # noqa 的行整行跳过
  * import * 跳过
"""
import ast
import io
import os
import sys


def _names_from_import(node):
    out = []
    for alias in node.names:
        if alias.name == "*":
            continue
        out.append(alias.asname or alias.name.split(".")[0])
    return out


def check_file(path, strict=False):
    src = io.open(path, encoding="utf-8").read()
    lines = src.split("\n")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [("<语法错误>", e.lineno, str(e))]
    imported = []          # (name, lineno)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for n in _names_from_import(node):
                imported.append((n, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for n in _names_from_import(node):
                imported.append((n, node.lineno))
    if not imported:
        return []
    # 用 AST 收集所有「被当名字用到」的标识符（比 grep 准，不会把注释算进去）
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            pass
    # 名字也可能出现在字符串注解 / docstring 里，保守起见也算（避免误报）
    "\n".join(lines)
    out = []
    for name, lineno in imported:
        line = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
        if "noqa" in line and not strict:
            continue
        if name in used:
            continue
        # 兜底：整个文件里再出现一次（排除 import 那一行本身）
        hits = [i for i, l in enumerate(lines)
                if i != lineno - 1 and name in l]
        if hits:
            continue
        out.append((name, lineno, line.strip()))
    return out


def iter_files(target):
    if os.path.isfile(target):
        yield target
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "target")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


def main(argv):
    strict = "--strict" in argv
    argv = [a for a in argv if a != "--strict"]
    targets = argv or ["game_server", "shared", "web", "tests", "tools"]
    total = 0
    for t in targets:
        for path in iter_files(t):
            for name, lineno, text in check_file(path, strict=strict):
                total += 1
                print("%s:%d  未使用: %s   | %s" % (path, lineno, name, text[:80]))
    print("-" * 56)
    print("共 %d 处%s" % (total, "（严格口径：含 # noqa）" if strict else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
