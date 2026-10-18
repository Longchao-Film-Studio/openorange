# -*- coding: utf-8 -*-
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/dump_024_5305KB_net.bin"
data = open(F, "rb").read()
print("Assembly-CSharp 大小:", len(data))
print()

names = ["sendConnectServer", "connectResponse", "onMicroResponse", "sendMicroRequest",
         "sendRequestEncryptToken", "confirmEncryptKeyAck", "onConfirmEncryptKeyAck",
         "sendHeartbeat", "recvHeartbeat", "onMicroPush", "bindAccount", "bindAvatar",
         "bindSoul", "unbindSoul", "disconnectClient", "GameLoadService", "ClientGate",
         "syncMetadata", "ERR_CONN_NEED_BIND", "msGate", "HandlerType"]
for n in names:
    b = n.encode()
    idxs = []
    s = 0
    while True:
        i = data.find(b, s)
        if i < 0:
            break
        idxs.append(i)
        s = i + 1
        if len(idxs) >= 5:
            break
    if idxs:
        print("%-26s %d 处: %s" % (n, len(idxs), ", ".join("0x%X" % x for x in idxs)))
    else:
        print("%-26s 未找到" % n)