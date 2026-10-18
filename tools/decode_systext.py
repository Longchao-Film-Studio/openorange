# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BS = chr(92)
Q = chr(34)
F = "F:/haowancheng/archive/game_data/LuaAll_Decompiled2/tabledata/commondata/systextdata.lua"
lines = open(F, encoding="utf-8", errors="replace").read().splitlines()

def dec(s):
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == BS and i + 3 < len(s) and s[i+1:i+4].isdigit():
            v = int(s[i+1:i+4])
            if v > 255:
                out.extend(s[i].encode("utf-8")); i += 1; continue
            out.append(v)
            i += 4
        else:
            out.extend(s[i].encode("utf-8"))
            i += 1
    return bytes(out).decode("utf-8", errors="replace")

ids = [35, 38, 56, 57, 126, 10459, 10460, 10461, 10462, 10463, 10464, 10465, 10466, 10467, 10468]
for tid in ids:
    marker = "[" + str(tid) + "] ="
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(marker):
            start = i
            break
    if start is None:
        print("  [%d] (未找到)" % tid); continue
    content = None
    for j in range(start, min(start + 8, len(lines))):
        s = lines[j].strip()
        if s.startswith("Content = " + Q):
            body = s[len("Content = " + Q):]
            content = body.rsplit(Q + ",", 1)[0]
            break
    print("  [%d] %s" % (tid, dec(content) if content else "(无 Content)"))