# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll"
data = open(F, "rb").read()
print("xlua.dll size =", len(data))

names = ["sendMicroRequest", "onMicroResponse", "sendConnectServer", "connectResponse",
         "sendRequestEncryptToken", "onConfirmEncryptKeyAck", "confirmEncryptKeyAck",
         "sendHeartbeat", "recvHeartbeat", "onMicroPush", "bindAccount", "bindAvatar",
         "bindSoul", "unbindSoul", "disconnectClient", "registerRpc", "genRpcIndex",
         "getRpcName", "getSendRpcName", "phonestcore"]

found = {}
for n in names:
    idxs = []
    start = 0
    while True:
        i = data.find(n.encode(), start)
        if i < 0:
            break
        idxs.append(i)
        start = i + 1
        if len(idxs) > 8:
            break
    found[n] = idxs
    if idxs:
        print("%-28s %d 处: %s" % (n, len(idxs), ", ".join("0x%X" % x for x in idxs)))
    else:
        print("%-28s 未找到" % n)