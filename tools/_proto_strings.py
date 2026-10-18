# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

# 在 .rdata 区找可打印字符串，筛出与 proto 相关的
strs = []
i = 0x5E9000
end = 0x7B0000
cur = bytearray()
start = i
while i < end and i < len(d):
    b = d[i]
    if 32 <= b < 127:
        if not cur:
            start = i
        cur.append(b)
    else:
        if len(cur) >= 6:
            strs.append((start, cur.decode("ascii")))
        cur = bytearray()
    i += 1

print("rdata 里长度>=6 的字符串:", len(strs))
print()

pat = re.compile(r"phonest|proto|Connect|Micro|Encrypt|Heartbeat|Bind|Soul|Channel|Entity|Ticket", re.I)
hits = [(o, s) for o, s in strs if pat.search(s)]
print("匹配 %d 条:" % len(hits))
for o, s in hits[:80]:
    print("  0x%06X  %s" % (o, s[:100]))