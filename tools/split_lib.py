#!/usr/bin/env python3
"""拆文件助手（P0-E2 专用，不参与运行时）。

entity_rpc.py 有 2600+ 行、87 个顶层定义。手工搬容易漏行、容易把
紧邻的注释切给错的函数，所以这里做成可复用的「整段取走」工具：

    take(src, "_dungeon_finish")  ->  (chunk, rest)

规则：
  1. 从顶层 `def NAME(` 或 `NAME = ` 开始；
  2. 往上吃掉紧邻的注释行（段落分隔线 `# ---` 不吃）；
  3. 到下一个顶层定义/段落线为止；
  4. 结尾那串注释若紧贴下一个定义，还给 rest（不抢别人的注释）。
"""
import re


def load(path):
    return open(path, encoding="utf-8").read()


def save(path, text):
    open(path, "w", encoding="utf-8", newline="\n").write(text)


_TOP = re.compile(r"^(def |[A-Za-z_][A-Za-z0-9_]* = |# ---|@)")


def _starts(lines):
    return [i for i, l in enumerate(lines) if _TOP.match(l)]


def take(src, name):
    """整段取走顶层定义 name，返回 (chunk, rest)。"""
    lines = src.split("\n")
    pat = re.compile(r"^(def {}\(|{} = )".format(re.escape(name), re.escape(name)))
    idx = [i for i, l in enumerate(lines) if pat.match(l)]
    if len(idx) != 1:
        raise SystemExit('take({}): 命中 {} 处'.format(name, len(idx)))
    head = idx[0]
    start = head
    while (start > 0 and lines[start - 1].startswith("#")
           and not lines[start - 1].startswith("# ---")):
        start -= 1
    end = None
    for s in _starts(lines):
        if s > head:
            end = s
            break
    if end is None:
        end = len(lines)
    # 结尾的注释还给 rest
    while end > head + 1 and lines[end - 1].startswith("#"):
        end -= 1
    chunk = "\n".join(lines[start:end]).rstrip("\n")
    rest = "\n".join(lines[:start] + lines[end:])
    return chunk, rest


def take_all(src, names):
    """按顺序取走多个定义，返回 (chunks, rest)。"""
    chunks = []
    for n in names:
        c, src = take(src, n)
        chunks.append(c)
    return chunks, src


def strip_blank_runs(text, keep=2):
    """把连续空行压到 keep 行以内（搬走一段后常留下大段空白）。"""
    out = re.sub(r"\n{%d,}" % (keep + 1), "\n" * keep, text)
    return out
