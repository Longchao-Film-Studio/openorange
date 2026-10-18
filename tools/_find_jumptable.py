# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def is_stub(off):
    return 0 <= off < len(d) - 5 and d[off] == 0xE9

def stub_target(off):
    rel = struct.unpack_from("<i", d, off + 1)[0]
    return (off + 5 + rel) & 0xFFFFFFFF

def find_table_base(hit):
    """从 hit 向前按 5 字节步长回退，直到不再是 E9 跳板"""
    s = hit
    while is_stub(s - 5):
        s -= 5
    return s

for hit, name in ((0x220D4, "connectResponse"), (0x1AD0C, "onMicroResponse")):
    base = find_table_base(hit)
    idx = (hit - base) // 5
    print("=== %s ===" % name)
    print("  命中偏移 0x%X" % hit)
    print("  表起点   0x%X" % base)
    print("  索引     %d  <== 这就是消息类型号！" % idx)
    print("  跳转目标 0x%X" % stub_target(hit))
    print()

print("=== 两张表的完整内容（索引 -> 目标 RVA）===")
for hit, name in ((0x220D4, "connectResponse 所在表"), (0x1AD0C, "onMicroResponse 所在表")):
    base = find_table_base(hit)
    # 向后也找一段
    e = hit
    while is_stub(e + 5):
        e += 5
    n = (e - base) // 5 + 1
    print("--- %s  起点 0x%X  共 %d 项 ---" % (name, base, n))
    for i in range(n):
        off = base + i * 5
        t = stub_target(off)
        mark = "   <== %s" % ("connectResponse" if off == 0x220D4 else ("onMicroResponse" if off == 0x1AD0C else ""))
        print("   [%3d] 0x%06X -> RVA 0x%06X%s" % (i, off, t, mark))
    print()