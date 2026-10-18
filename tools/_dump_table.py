# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()
BASE = 0x7FFEA39AF000   # 运行时基址（Ghidra 用的是 0x7ffea39b0000，差 0x1000）
GHIDRA_BASE = 0x7FFEA39B0000

print("文件里 0x17fa744 附近的数据（每 8 字节一个指针，转成 RVA）:")
start = 0x17FA600
end = 0x17FA800
for off in range(start, end, 8):
    v = struct.unpack_from("<Q", d, off)[0]
    rva = v - GHIDRA_BASE
    note = ""
    if 0 <= rva < len(d):
        note = "-> RVA 0x%X" % rva
        if 0x11ff40 <= rva <= 0x11ff80:
            note += "   <== connectResponse handler!"
    print("  %06X: 0x%016X  %s" % (off, v, note))