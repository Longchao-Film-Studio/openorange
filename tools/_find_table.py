# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

# 注意：我们 dump 时把 PointerToRawData 设成了 VirtualAddress(RVA)，
# 所以文件偏移 == RVA。
targets = {
    "sendConnectServer": 0x5FD608,
    "connectResponse": 0x5FE9B8,
    "onMicroResponse": 0x5FBBD8,
    "sendMicroRequest": 0x5F7E68,
    "sendRequestEncryptToken": 0x5F7E80,
    "confirmEncryptKeyAck": 0x5F56FA,
}

print("=== 搜 8 字节指针（RVA 形式）===")
for name, rva in targets.items():
    pat8 = struct.pack("<Q", rva)
    pat4 = struct.pack("<I", rva)
    hits8 = []
    s = 0
    while len(hits8) < 4:
        i = d.find(pat8, s)
        if i < 0: break
        hits8.append(i); s = i + 1
    hits4 = []
    s = 0
    while len(hits4) < 4:
        i = d.find(pat4, s)
        if i < 0: break
        hits4.append(i); s = i + 1
    print("%-24s RVA=0x%06X  ptr8:%s  val4:%s" % (
        name, rva,
        ", ".join("0x%X" % x for x in hits8) or "-",
        ", ".join("0x%X" % x for x in hits4) or "-"))

print()
print("=== 字符串区域布局（0x5FD5C0 - 0x5FD700）===")
start = 0x5FD5C0
for off in range(start, 0x5FD700, 16):
    chunk = d[off:off+16]
    hx = " ".join("%02X" % b for b in chunk)
    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    print("  %06X  %-47s  |%s|" % (off, hx, asc))