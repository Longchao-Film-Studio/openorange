# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()
TEXT_START, TEXT_END = 0x1000, 0x1000 + 0x5E7CBB

targets = {
    0x5FFBE7: "registerRpc",
    0x61A928: "genRpcIndex",
    0x61A910: "getSendRpcName",
    0x5FFCDF: "getRpcName",
    0x5EB238: "dispatchRpc",
    0x5EF430: "phonestcore",
}
hits = {k: [] for k in targets}
for q in range(TEXT_START, min(TEXT_END, len(d) - 4)):
    v = struct.unpack_from("<i", d, q)[0]
    tgt = (q + 4 + v) & 0xFFFFFFFF
    if tgt in hits:
        hits[tgt].append(q)

for tgt, name in targets.items():
    print("%-16s (0x%X) 引用点: %s" % (name, tgt,
          ", ".join("0x%X" % x for x in hits[tgt][:8]) or "-"))

def dump(center, before=64, after=48, tag=""):
    s = max(0, center - before); e = min(len(d), center + after)
    print()
    print("=== %s  @0x%X ===" % (tag, center))
    for off in range(s, e, 16):
        chunk = d[off:off+16]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("  %06X  %-47s |%s|" % (off, hx, asc))

if hits[0x5FFBE7]:
    dump(hits[0x5FFBE7][0], 64, 48, "registerRpc 首次引用")