# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/work/ghidra_script_out.txt"
code = open(F, encoding="utf-8").read()

marks = [(m.start(), m.group(1), m.group(2)) for m in re.finditer(r"=== rva 0x([0-9a-f]+)\s+->\s+function (\S+)", code)]
print("找到 %d 个函数标记" % len(marks))
for s, rva, fn in marks:
    print("   0x%-8s %s" % (rva, fn))
print()

def show(rva_key, limit=8000):
    for i, (s, rva, fn) in enumerate(marks):
        if rva == rva_key:
            e = marks[i+1][0] if i+1 < len(marks) else len(code)
            body = code[s:e]
            print("=" * 72)
            print("=== rva 0x%s  %s ===" % (rva, fn))
            print("=" * 72)
            print(body[:limit])
            return
    print("未找到 rva", rva_key)

show("1231ce", 9000)