# -*- coding: utf-8 -*-
import sys, io, re, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/ghidra/out/analysis.log"
txt = open(LOG, encoding="utf-8", errors="replace").read()
print("日志大小:", len(txt))

# 提取脚本输出行（去掉前缀）
lines = []
for ln in txt.splitlines():
    m = re.search(r"DecompileAt\.java> (.*)$", ln)
    if m:
        lines.append(m.group(1))
    elif "DecompileAt.java>" in ln:
        lines.append("")
code = "\n".join(lines)
print("脚本输出行数:", len(lines))

open(r"F:/haowancheng/work/ghidra_script_out.txt", "w", encoding="utf-8").write(code)
print("已保存到 F:/haowancheng/work/ghidra_script_out.txt")
print()

proto = sorted(set(re.findall(r"phonest::proto::[A-Za-z0-9_]+", code)))
print("=== phonest::proto:: 类 ===")
for p in proto:
    print("  ", p)
if not proto:
    print("   (无)")
print()

funcs = re.findall(r"=== rva 0x([0-9a-f]+)\s+->\s+function (\S+)", code)
print("=== 反编译到的函数 ===")
for rva, fn in funcs:
    print("   rva 0x%s  %s" % (rva, fn))