# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

# 验证：lua_load 真实体日志为 0x7ffea39f6e00，假定 RVA 0x46E00 -> 基址 0x7FFEA39AF000
print("=== 验证 RVA 0x46E00 处是否是 lua_load 真实体 ===")
exp = bytes.fromhex("48895C2408574883EC5049 8BD9488BF9".replace(" ", ""))
got = d[0x46E00:0x46E00+len(exp)]
print("  期望:", exp.hex(" "))
print("  实际:", got.hex(" "))
print("  匹配:", got == exp)
print()

def dump(center, before=64, after=48, tag=""):
    s = max(0, center - before); e = min(len(d), center + after)
    print("=== %s  @0x%X ===" % (tag, center))
    for off in range(s, e, 16):
        chunk = d[off:off+16]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("  %06X  %-47s |%s|" % (off, hx, asc))
    print()

dump(0x22CF61, 80, 48, "genRpcIndex 引用")
dump(0x22CD19, 80, 48, "getSendRpcName 引用")
dump(0x22DFAF, 80, 48, "dispatchRpc 引用")