# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll"
data = open(F, "rb").read()

hashes = {
    "connectResponse": 2119322371,
    "sendConnectServer": 2087431183,
    "sendRequestEncryptToken": 1378977164,
    "confirmEncryptKeyAck": 543775382,
    "sendHeartbeat": 1574166541,
    "bindAccount": 889905872,
    "onMicroResponse": None,
    "sendMicroRequest": None,
}

print("在 xlua.dll 中搜索哈希常量（小端 4 字节）")
for name, h in hashes.items():
    if h is None:
        continue
    le = struct.pack("<I", h)
    be = struct.pack(">I", h)
    ile = data.find(le)
    ibe = data.find(be)
    print("%-26s 0x%08X  LE@%s  BE@%s" % (
        name, h,
        ("0x%X" % ile) if ile >= 0 else "-",
        ("0x%X" % ibe) if ibe >= 0 else "-"))

print()
print("也搜 GameAssembly.dll：")
G = r"F:/haowancheng/GuiLongchao/game/GameAssembly.dll"
g = open(G, "rb").read()
print("GameAssembly.dll size =", len(g))
for name, h in hashes.items():
    if h is None:
        continue
    le = struct.pack("<I", h)
    ile = g.find(le)
    print("%-26s 0x%08X  LE@%s" % (name, h, ("0x%X" % ile) if ile >= 0 else "-"))