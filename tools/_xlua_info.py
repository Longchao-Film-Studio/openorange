# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll"
d = open(F, "rb").read()
print("xlua.dll 大小:", len(d), "字节")

pe = struct.unpack_from("<I", d, 0x3C)[0]
print("PE 头 @0x%X  签名=%s" % (pe, d[pe:pe+4]))

coff = pe + 4
num_sec = struct.unpack_from("<H", d, coff + 2)[0]
opt_size = struct.unpack_from("<H", d, coff + 16)[0]
opt = coff + 20
magic = struct.unpack_from("<H", d, opt)[0]
print("节数:", num_sec, " 可选头魔数: 0x%X" % magic)

# 导出表
if magic == 0x20B:
    dir_off = opt + 112
else:
    dir_off = opt + 96
exp_rva = struct.unpack_from("<I", d, dir_off)[0]
exp_size = struct.unpack_from("<I", d, dir_off + 4)[0]
print("导出目录 RVA=0x%X size=%d" % (exp_rva, exp_size))

# 节表
sec = opt + opt_size
secs = []
for i in range(num_sec):
    o = sec + i * 40
    name = d[o:o+8].rstrip(b"\0").decode("ascii", "replace")
    vsize, vaddr, rsize, roff = struct.unpack_from("<IIII", d, o + 8)
    secs.append((name, vaddr, vsize, roff, rsize))
    print("  %-10s VA=0x%08X VSize=0x%08X RawOff=0x%08X RawSize=0x%08X"
          % (name, vaddr, vsize, roff, rsize))

def rva2off(rva):
    for name, vaddr, vsize, roff, rsize in secs:
        if vaddr <= rva < vaddr + max(vsize, rsize):
            return roff + (rva - vaddr)
    return None

eo = rva2off(exp_rva)
if eo:
    n_names = struct.unpack_from("<I", d, eo + 24)[0]
    names_rva = struct.unpack_from("<I", d, eo + 32)[0]
    no = rva2off(names_rva)
    names = []
    for i in range(min(n_names, 500)):
        nr = struct.unpack_from("<I", d, no + i*4)[0]
        npo = rva2off(nr)
        e = d.index(b"\0", npo)
        names.append(d[npo:e].decode("ascii", "replace"))
    print()
    print("导出函数数:", n_names)
    key = [n for n in names if n.startswith("lua") or n.startswith("xlua") or "phonest" in n]
    print("其中 lua*/xlua*/phonest* 共", len(key), "个，样本：")
    for n in key[:25]:
        print("   ", n)
    print("    ...")
    print("含 phonest 的导出:", [n for n in names if "phonest" in n.lower()])