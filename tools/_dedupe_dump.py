# -*- coding: utf-8 -*-
import os, glob, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
D = r"F:/haowancheng/OpenOrange/launcher/dist"
by = {}
for fp in sorted(glob.glob(os.path.join(D, "dump_*.bin"))):
    sz = os.path.getsize(fp)
    by.setdefault(sz, []).append(fp)
removed = 0
for sz, files in by.items():
    for fp in files[1:]:
        os.remove(fp)
        removed += 1
print("去重删除", removed, "个，保留", sum(len(v) for v in by.values()), "个")