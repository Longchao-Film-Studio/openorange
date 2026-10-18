# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

# .text: VA 0x1000, VSize 0x5E7CBB
TEXT_START, TEXT_END = 0x1000, 0x1000 + 0x5E7CBB

targets = {
    0x5FD608: "sendConnectServer",
    0x5FE9B8: "connectResponse",
    0x5FBBD8: "onMicroResponse",
    0x5F7E68: "sendMicroRequest",
    0x5F7E80: "sendRequestEncryptToken",
    0x5F56FA: "confirmEncryptKeyAck",
    0x5FD650: "sendHeartbeat",
    0x5FEB48: "recvHeartbeat",
    0x5FBBF0: "onMicroPush",
}

hits = {k: [] for k in targets}
n = 0
for q in range(TEXT_START, min(TEXT_END, len(d) - 4)):
    v = struct.unpack_from("<i", d, q)[0]
    tgt = (q + 4 + v) & 0xFFFFFFFF
    if tgt in hits:
        hits[tgt].append(q)
        n += 1

print("共找到 %d 处 RIP 相对引用" % n)
for tgt, name in targets.items():
    print("  %-24s (0x%X) 引用点: %s" % (name, tgt,
          ", ".join("0x%X" % x for x in hits[tgt][:6]) or "-"))