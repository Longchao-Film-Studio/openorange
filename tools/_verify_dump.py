# -*- coding: utf-8 -*-
import sys, io, os, glob, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = r"F:/haowancheng/OpenOrange/launcher/dist"
cands = sorted(glob.glob(os.path.join(D, "dump_*_net.bin")))
print("候选 .NET 程序集:", len(cands))
seen = set()
for fp in cands:
    data = open(fp, "rb").read()
    if data[:2] != b"MZ":
        continue
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e:e+4] != b"PE\0\0":
        continue
    key = (len(data), data[:64])
    if key in seen:
        continue
    seen.add(key)
    # 找 BSJB（.NET 元数据签名）
    bsjb = data.find(b"BSJB")
    # 读程序集名：在 #Strings 流里通常紧跟 BSJB 之后
    name = ""
    if bsjb > 0:
        # 粗略：BSJB 后 16 字节是版本号，再往后是字符串堆偏移；这里直接找可打印名字
        pass
    # 用 PE 时间戳/大小做标识
    print("%-28s %8d 字节  BSJB@%s" % (os.path.basename(fp), len(data),
          ("0x%X" % bsjb) if bsjb > 0 else "-"))