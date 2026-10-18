# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/ghidra/out/xrefs.log"
txt = open(LOG, encoding="utf-8", errors="replace").read()
i = txt.find("##########")
seg = txt[i:] if i >= 0 else txt

pref = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +(?:\([^)]*\) )?XrefsTo\.java> ?(.*)$")
other = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \w+ +\(")
out = []
for ln in seg.splitlines():
    m = pref.match(ln)
    if m:
        out.append(m.group(1)); continue
    if other.match(ln):
        continue
    out.append(ln)
code = "\n".join(out)
open(r"F:/haowancheng/work/ghidra_xrefs_out.txt", "w", encoding="utf-8").write(code)
print("提取长度:", len(code))
print()
# 只看引用列表部分
for m in re.finditer(r"########## xrefs to rva (0x[0-9a-f]+) ##########\n(.*?)(?=##########|----- DECOMPILE)", code, re.S):
    print("### xrefs to", m.group(1))
    print(m.group(2).strip()[:1500])
    print()