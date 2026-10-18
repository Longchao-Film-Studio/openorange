# -*- coding: utf-8 -*-
import sys, io, os, glob, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = r"F:/haowancheng/OpenOrange/launcher/dist"
uniq = {}
for fp in sorted(glob.glob(os.path.join(D, "dump_*_net.bin"))):
    d = open(fp, "rb").read()
    if d[:2] == b"MZ":
        uniq.setdefault(len(d), fp)

def meta(d):
    i = d.find(b"BSJB")
    if i < 0:
        return None, None
    off = i + 4 + 4 + 4
    vlen = struct.unpack_from("<I", d, off)[0]; off += 4
    off += (vlen + 3) & ~3
    off += 2
    nstreams = struct.unpack_from("<H", d, off)[0]; off += 2
    streams = {}
    for _ in range(nstreams):
        so = struct.unpack_from("<I", d, off)[0]; off += 4
        sz = struct.unpack_from("<I", d, off)[0]; off += 4
        e = d.index(b"\0", off)
        nm = d[off:e].decode("ascii", "replace")
        off = e + 1
        off = (off + 3) & ~3
        streams[nm] = (i + so, sz)   # ★ 偏移相对元数据根
    return streams, i

print("%-30s %9s  %-44s" % ("file", "size", "module name"))
for size in sorted(uniq, reverse=True):
    fp = uniq[size]
    d = open(fp, "rb").read()
    st, root = meta(d)
    if not st or "#Strings" not in st:
        print("%-30s %9d  ?" % (os.path.basename(fp), size))
        continue
    so, sz = st["#Strings"]
    e = d.index(b"\0", so)
    mod = d[so:e].decode("utf-8", "replace")
    print("%-30s %9d  %s" % (os.path.basename(fp), size, mod[:44]))