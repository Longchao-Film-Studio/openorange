# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def dump(center, before=80, after=48, tag=""):
    s = max(0, center - before)
    e = min(len(d), center + after)
    print("=== %s  引用点 0x%X  (窗口 0x%X-0x%X) ===" % (tag, center, s, e))
    for off in range(s, e, 16):
        chunk = d[off:off+16]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        mark = " <== disp" if off <= center < off+16 else ""
        print("  %06X  %-47s |%s|%s" % (off, hx, asc, mark))
    print()

dump(0x11FF80, 96, 64, "connectResponse")
dump(0x10DB44, 96, 64, "onMicroResponse")
dump(0x1231CE, 96, 64, "sendConnectServer (第二处)")