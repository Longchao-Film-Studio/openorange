"""把内存 dump 重建成合法 PE（file offset == RVA），供 Ghidra 正常导入。

原理：dump 出来的镜像里，每个段的字节就位于它的 VirtualAddress 处。
所以只要把段表的 PointerToRawData 改成 VirtualAddress、
SizeOfRawData 改成 VirtualSize，整个文件就是「偏移 = RVA」的布局。
"""
import struct, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DUMP = r"F:\ghidra\out\xlua_dump.bin"
OUT = r"F:\ghidra\out\xlua_unpacked.dll"

img = bytearray(open(DUMP, 'rb').read())
e_lfanew = struct.unpack_from("<I", img, 0x3C)[0]
coff = e_lfanew + 4
nsec, = struct.unpack_from('<H', img, coff + 2)
opt_size, = struct.unpack_from('<H', img, coff + 16)
opt = coff + 20
magic, = struct.unpack_from('<H', img, opt)
assert magic == 0x20b, hex(magic)
size_of_image, = struct.unpack_from('<I', img, opt + 56)
size_of_headers, = struct.unpack_from('<I', img, opt + 60)
sect = opt + opt_size

print("SizeOfImage=%#x SizeOfHeaders=%#x sections=%d" % (size_of_image, size_of_headers, nsec))
for i in range(nsec):
    o = sect + i * 40
    nm = img[o:o+8].rstrip(b'\x00').decode('ascii', 'replace')
    vsize, vaddr, rawsize, rawptr = struct.unpack_from('<IIII', img, o + 8)
    # 关键：让文件偏移 = RVA
    struct.pack_into('<I', img, o + 16, vaddr)   # PointerToRawData
    struct.pack_into('<I', img, o + 20, vsize)   # SizeOfRawData
    print("   %-10s va=%#010x vs=%#x -> raw=%#x rsz=%#x" % (nm, vaddr, vsize, vaddr, vsize))

with open(OUT, 'wb') as f:
    f.write(img)
print("已写出 %s (%d 字节)" % (OUT, len(img)))