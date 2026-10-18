# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/ghidra/out/analysis.log"
txt = open(LOG, encoding="utf-8", errors="replace").read()
i = txt.find("imageBase =")
seg = txt[i:]

pref = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +(?:\([^)]*\) )?DecompileAt\.java> ?(.*)$")
other = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +\(")

out = []
for ln in seg.splitlines():
    m = pref.match(ln)
    if m:
        out.append(m.group(1))
        continue
    if other.match(ln):
        continue
    out.append(ln)

code = "\n".join(out)
open(r"F:/haowancheng/work/ghidra_script_out.txt", "w", encoding="utf-8").write(code)
print("提取后长度:", len(code))

proto = sorted(set(re.findall(r"phonest::proto::[A-Za-z0-9_]+", code)))
print()
print("=== phonest::proto:: 类 ===")
for p in proto:
    print("  ", p)
if not proto:
    print("   (无)")

print()
print("=== 函数清单 ===")
for rva, fn in re.findall(r"=== rva 0x([0-9a-f]+)\s+->\s+function (\S+)", code):
    print("   rva 0x%s  %s" % (rva, fn))