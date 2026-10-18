# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()
print("脱壳后大小:", len(d), "字节")
print()

names = ["dispatchRpc", "sendConnectServer", "connectResponse", "onMicroResponse",
         "sendMicroRequest", "sendRequestEncryptToken", "confirmEncryptKeyAck",
         "onConfirmEncryptKeyAck", "sendHeartbeat", "recvHeartbeat", "onMicroPush",
         "bindAccount", "bindAvatar", "bindSoul", "unbindSoul", "disconnectClient",
         "registerRpc", "genRpcIndex", "getRpcName", "getSendRpcName",
         "phonestcore", "GameLoadService", "ClientGate", "syncMetadata",
         "sendMicroResponse", "onMicroRequest"]
for n in names:
    for enc, tag in ((n.encode(), "ascii"), (n.encode("utf-16-le"), "utf16")):
        i = d.find(enc)
        if i >= 0:
            print("%-26s [%s] @0x%X" % (n, tag, i))
            break
    else:
        print("%-26s 未找到" % n)