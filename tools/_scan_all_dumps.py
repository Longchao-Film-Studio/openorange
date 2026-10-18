# -*- coding: utf-8 -*-
import sys, io, os, glob, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = r"F:/haowancheng/OpenOrange/launcher/dist"
cands = sorted(glob.glob(os.path.join(D, "dump_*_net.bin")))

# 去重
uniq = {}
for fp in cands:
    d = open(fp, "rb").read()
    if d[:2] != b"MZ":
        continue
    uniq.setdefault(len(d), fp)
print("去重后 %d 个程序集" % len(uniq))
print()

names = ["sendConnectServer", "connectResponse", "onMicroResponse", "sendMicroRequest",
         "sendRequestEncryptToken", "confirmEncryptKeyAck", "GameLoadService",
         "ClientGate", "syncMetadata", "ERR_CONN_NEED_BIND", "onMicroPush"]

for size in sorted(uniq, reverse=True):
    fp = uniq[size]
    d = open(fp, "rb").read()
    hits = []
    for n in names:
        for enc, tag in ((n.encode(), "ascii"), (n.encode("utf-16-le"), "utf16")):
            if d.find(enc) >= 0:
                hits.append(n + "/" + tag)
    if hits:
        print("%-30s %8d 字节  命中: %s" % (os.path.basename(fp), size, ", ".join(hits)))

print()
print("=== 若上面无输出，则逐个列出程序集名（从元数据 #Strings 提取）===")

def assembly_name(d):
    # BSJB 后：Version(4) + Length + Flags + 4 个流偏移；再找 #Strings 流
    i = d.find(b"BSJB")
    if i < 0:
        return "?"
    try:
        off = i + 16
        vlen = struct.unpack_from("<I", d, off)[0]
        off += 4 + vlen
        nstreams = struct.unpack_from("<H", d, off)[0]
        off += 2
        streams = {}
        for _ in range(nstreams):
            so = struct.unpack_from("<I", d, off)[0]; off += 4
            sz = struct.unpack_from("<I", d, off)[0]; off += 4
            e = d.index(b"\0", off)
            nm = d[off:e].decode("ascii", "replace")
            off = e + 1
            off = (off + 3) & ~3
            streams[nm] = (so, sz)
        if "#Strings" not in streams:
            return "?"
        so, sz = streams["#Strings"]
        # 模块名通常是第一个字符串
        end = d.index(b"\0", so)
        return d[so:end].decode("utf-8", "replace")
    except Exception as ex:
        return "?(" + str(ex) + ")"

for size in sorted(uniq, reverse=True):
    fp = uniq[size]
    d = open(fp, "rb").read()
    print("%-30s %8d 字节  name=%s" % (os.path.basename(fp), size, assembly_name(d)))