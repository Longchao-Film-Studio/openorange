# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def dump_strings(s, e, minlen=2):
    i = s; cur = bytearray(); start = i; out = []
    while i < e:
        b = d[i]
        if 32 <= b < 127:
            if not cur: start = i
            cur.append(b)
        else:
            if len(cur) >= minlen: out.append((start, cur.decode("ascii")))
            cur = bytearray()
        i += 1
    if len(cur) >= minlen: out.append((start, cur.decode("ascii")))
    return out

print("=== 0x5F6300 - 0x5F6A00 ===")
for off, s in dump_strings(0x5F6300, 0x5F6A00):
    print("  0x%06X  %s" % (off, s[:100]))