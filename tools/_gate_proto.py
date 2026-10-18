# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def dump_strings(s, e):
    i = s
    cur = bytearray()
    start = i
    out = []
    while i < e:
        b = d[i]
        if 32 <= b < 127:
            if not cur:
                start = i
            cur.append(b)
        else:
            if len(cur) >= 3:
                out.append((start, cur.decode("ascii")))
            cur = bytearray()
        i += 1
    if len(cur) >= 3:
        out.append((start, cur.decode("ascii")))
    return out

print("=== client_gate.proto 描述符区域的字符串 (0x61DA80 - 0x61E200) ===")
for off, s in dump_strings(0x61DA80, 0x61E200):
    print("  0x%06X  %s" % (off, s[:110]))