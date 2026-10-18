# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/ghidra/out/bind.log"
txt = open(LOG, encoding="utf-8", errors="replace").read()
i = txt.find("imageBase =")
seg = txt[i:] if i >= 0 else txt
pref = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +(?:\([^)]*\) )?DecompileAt\.java> ?(.*)$")
other = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +\(")
out = []
for ln in seg.splitlines():
    m = pref.match(ln)
    if m: out.append(m.group(1)); continue
    if other.match(ln): continue
    out.append(ln)
code = "\n".join(out)
open(r"F:/haowancheng/work/ghidra_bind_out.txt", "w", encoding="utf-8").write(code)
print("提取长度:", len(code))
print()
# 只打印 bindAccount 那段
s = code.find("rva 0x11fbb0")
e = code.find("rva 0x11fe30")
print(code[max(0,s-200):e if e>0 else len(code)][:6000])