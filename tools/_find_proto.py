# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

# 找所有 .proto 文件名
print("=== 所有 .proto 文件名 ===")
seen = set()
for m in re.finditer(rb"[A-Za-z0-9_\./\\-]+\.proto", d):
    s = m.group(0).decode("ascii", "replace")
    base = s.split("\\")[-1].split("/")[-1]
    if base not in seen:
        seen.add(base)
        print("  0x%06X  %s" % (m.start(), base))

print()
print("=== 找 gate 相关 proto 包名/消息名 ===")
pat = re.compile(rb"phonest\.gate[A-Za-z0-9_\.]*|gate[A-Za-z0-9_]*\.proto|ConnectServer[A-Za-z]*|ConnectResponse[A-Za-z]*")
seen2 = set()
for m in pat.finditer(d):
    s = m.group(0).decode("ascii", "replace")
    if s not in seen2:
        seen2.add(s)
        print("  0x%06X  %s" % (m.start(), s))