# -*- coding: utf-8 -*-
import os, sys, io, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

d = r"F:/haowancheng/OpenOrange/work/captures"
files = sorted(glob.glob(os.path.join(d, "*.bin")))
print("captures:", len(files))

for fp in files:
    data = open(fp, "rb").read()
    print()
    print("=== %s (%d bytes) ===" % (os.path.basename(fp), len(data)))
    pos = 0
    n = 0
    while pos < len(data) and n < 12:
        nl = data.find(b"\n", pos, pos + 48)
        if nl < 0:
            break
        try:
            tag, dt, ln = data[pos:nl].decode().split(" ")
            ln = int(ln)
        except Exception:
            break
        body = data[nl+1:nl+1+ln]
        print("  %-3s len=%-5d head=%s" % (tag, ln, " ".join("%02X" % b for b in body[:20])))
        pos = nl + 1 + ln
        n += 1
        if n >= 12:
            print("  ...")