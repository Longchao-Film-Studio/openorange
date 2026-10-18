# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def dump(center, before=64, after=96, tag=""):
    s = max(0, center - before); e = min(len(d), center + after)
    print("=== %s  @0x%X ===" % (tag, center))
    for off in range(s, e, 16):
        chunk = d[off:off+16]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        mark = "  <== xref" if off <= center < off+16 else ""
        print("  %06X  %-47s |%s|%s" % (off, hx, asc, mark))
    print()

dump(0x220D4, 64, 96, "connectResponse 的代码引用")
dump(0x1AD0C, 64, 96, "onMicroResponse 的代码引用")