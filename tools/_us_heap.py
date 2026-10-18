# -*- coding: utf-8 -*-
import sys, io, os, glob, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = r"F:/haowancheng/OpenOrange/launcher/dist"

def streams(d):
    i = d.find(b"BSJB")
    if i < 0:
        return None, None
    off = i + 12
    vlen = struct.unpack_from("<I", d, off)[0]; off += 4
    off += (vlen + 3) & ~3
    off += 2
    n = struct.unpack_from("<H", d, off)[0]; off += 2
    out = {}
    for _ in range(n):
        so = struct.unpack_from("<I", d, off)[0]; off += 4
        sz = struct.unpack_from("<I", d, off)[0]; off += 4
        e = d.index(b"\0", off)
        nm = d[off:e].decode("ascii", "replace")
        off = (e + 1 + 3) & ~3
        out[nm] = (i + so, sz)
    return out, i

def read_compressed(d, off):
    b = d[off]
    if b & 0x80 == 0:
        return b, off + 1
    if b & 0xC0 == 0x80:
        return ((b & 0x3F) << 8) | d[off+1], off + 2
    return ((b & 0x1F) << 24) | (d[off+1] << 16) | (d[off+2] << 8) | d[off+3], off + 4

def us_strings(d, so, sz, limit=200000):
    out = []
    off = so
    end = so + sz
    while off < end and len(out) < limit:
        try:
            ln, nxt = read_compressed(d, off)
        except Exception:
            break
        if ln == 0:
            off = nxt
            continue
        raw = d[nxt:nxt+ln]
        off = nxt + ln
        if not raw:
            continue
        try:
            s = raw[:-1].decode("utf-16-le", "replace")
        except Exception:
            continue
        out.append(s)
    return out

F = os.path.join(D, "dump_024_5305KB_net.bin")
d = open(F, "rb").read()
st, root = streams(d)
print("Assembly-CSharp streams:", sorted(st))

if "#US" in st:
    so, sz = st["#US"]
    ss = us_strings(d, so, sz)
    print("#US 字符串数:", len(ss))
    pats = ["ConnectServer", "connectResponse", "MicroResponse", "MicroRequest",
            "EncryptToken", "Heartbeat", "bindAccount", "bindAvatar", "bindSoul",
            "GameLoadService", "ClientGate", "syncMetadata", "Micro", "Connect"]
    for p in pats:
        hits = [s for s in ss if p in s]
        if hits:
            print("  命中 %-18s : %s" % (p, " | ".join(hits[:6])))
        else:
            print("  未命中 %-16s" % p)
else:
    print("无 #US 流")