# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def stub_target(off):
    if d[off] != 0xE9:
        return None
    rel = struct.unpack_from("<i", d, off + 1)[0]
    return (off + 5 + rel) & 0xFFFFFFFF

# 已知的处理函数 RVA（来自之前 Ghidra 反编译）
known = {
    0x11FF40: "connectResponse",
    0x10DB20: "onMicroResponse",
    0x11AD90: "send(内部发送)",
}

# 扫描 0x0348E0 起的注册函数，配对 lea 目标与紧随其后的 C7 立即数
start = 0x0348E0
end = 0x035200
entries = []
q = start
while q < end - 16:
    # 匹配 48 8D 05 <disp32>
    if d[q] == 0x48 and d[q+1] == 0x8D and d[q+2] == 0x05:
        disp = struct.unpack_from("<i", d, q + 3)[0]
        stub = (q + 7 + disp) & 0xFFFFFFFF
        # 往后 40 字节内找 C7 44 24 xx <imm32> 或 C7 45 xx <imm32>
        typ = None
        for k in range(q + 7, min(q + 40, end - 8)):
            if d[k] == 0xC7 and d[k+1] == 0x44 and d[k+2] == 0x24:
                typ = struct.unpack_from("<I", d, k + 4)[0]
                break
            if d[k] == 0xC7 and d[k+1] == 0x45:
                typ = struct.unpack_from("<I", d, k + 3)[0]
                break
        if typ is not None and stub < len(d):
            tgt = stub_target(stub)
            entries.append((typ, stub, tgt))
        q += 7
        continue
    q += 1

print("解出 %d 条注册记录" % len(entries))
print()
print("%-5s %-10s %-10s %s" % ("类型", "跳板RVA", "目标RVA", "备注"))
seen = set()
for typ, stub, tgt in entries:
    if (typ, stub) in seen:
        continue
    seen.add((typ, stub))
    note = known.get(tgt, "")
    print("%-5d 0x%08X 0x%08X %s" % (typ, stub, tgt if tgt is not None else -1, note))