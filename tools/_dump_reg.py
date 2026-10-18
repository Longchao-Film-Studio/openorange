# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def dump(s, e, tag):
    print("=== %s  (0x%X-0x%X) ===" % (tag, s, e))
    for off in range(s, e, 16):
        chunk = d[off:off+16]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("  %06X  %-47s |%s|" % (off, hx, asc))
    print()

dump(0x034880, 0x034A60, "注册函数区域")