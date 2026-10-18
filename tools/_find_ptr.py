# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()
BASE = 0x7FFEA39AF000   # = body 0x7ffea39f6e00 - RVA 0x46E00
print("模块基址 = 0x%X" % BASE)

# 用一条已知指针验证基址：文件里 0x5FD5C0 处是 F8 80 FA A3 FE 7F 00 00 = 0x7FFEA3FA80F8
probe = struct.unpack_from("<Q", d, 0x5FD5C0)[0]
print("验证: 0x5FD5C0 处指针 = 0x%X -> RVA 0x%X %s" % (
    probe, probe - BASE, "(在映像内)" if 0 <= probe - BASE < len(d) else "(越界)"))
print()

targets = {
    "sendConnectServer": 0x5FD608,
    "connectResponse": 0x5FE9B8,
    "onMicroResponse": 0x5FBBD8,
    "sendMicroRequest": 0x5F7E68,
    "sendRequestEncryptToken": 0x5F7E80,
    "confirmEncryptKeyAck": 0x5F56FA,
    "sendHeartbeat": 0x5FD650,
    "recvHeartbeat": 0x5FEB48,
}

print("=== 搜绝对指针（BASE + RVA）===")
for name, rva in targets.items():
    va = BASE + rva
    pat = struct.pack("<Q", va)
    hits = []
    s = 0
    while len(hits) < 6:
        i = d.find(pat, s)
        if i < 0: break
        hits.append(i); s = i + 1
    print("%-24s VA=0x%X  引用处: %s" % (name, va,
          ", ".join("0x%X" % x for x in hits) or "-"))