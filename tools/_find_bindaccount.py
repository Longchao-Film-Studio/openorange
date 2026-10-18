# -*- coding: utf-8 -*-
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = r"F:/haowancheng/archive/game_data/LuaAll_Decompiled2"
pats = ["bindAccount", "BindAccount", "onlineInfoWithBinding", "bindAccountHandler"]
hits = {p: [] for p in pats}
for root, dirs, files in os.walk(BASE):
    for fn in files:
        if not fn.endswith(".lua"):
            continue
        fp = os.path.join(root, fn)
        try:
            txt = open(fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for p in pats:
            if p in txt:
                for i, ln in enumerate(txt.splitlines(), 1):
                    if p in ln:
                        hits[p].append((fp.replace(BASE, ""), i, ln.strip()[:95]))
for p in pats:
    print("=== %s : %d 处 ===" % (p, len(hits[p])))
    for fp, i, ln in hits[p][:10]:
        print("  %s :L%d  %s" % (fp, i, ln))
    print()