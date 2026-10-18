# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()
TEXT_START, TEXT_END = 0x1000, 0x1000 + 0x5E7CBB

# 找 lea r64,[rip+disp32]：48 8D /r，ModRM 的 mod=00 rm=101
hits = []
for q in range(TEXT_START, min(TEXT_END, len(d) - 7)):
    if d[q] != 0x48 or d[q+1] != 0x8D:
        continue
    modrm = d[q+2]
    if (modrm & 0xC7) != 0x05:      # mod=00, rm=101 -> RIP 相对
        continue
    disp = struct.unpack_from("<i", d, q + 3)[0]
    tgt = (q + 7 + disp) & 0xFFFFFFFF
    # 表区域大致在 0x20000 - 0x24000
    if 0x1F000 <= tgt <= 0x25000:
        hits.append((q, modrm, tgt))

print("找到 %d 处指向 0x1F000-0x25000 的 lea" % len(hits))
for q, modrm, tgt in hits[:40]:
    # 看后面 16 字节是否含 jmp reg (FF /4)
    tail = d[q+7:q+7+16]
    hexs = " ".join("%02X" % b for b in tail)
    isjmp = any(tail[i] == 0xFF and (tail[i+1] & 0x38) == 0x20 for i in range(len(tail)-1))
    print("  lea@0x%06X reg=%d -> 0x%06X  后续: %s %s" % (
        q, (modrm >> 3) & 7, tgt, hexs, "<== 含 jmp reg" if isjmp else ""))