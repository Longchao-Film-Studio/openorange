# -*- coding: utf-8 -*-
import sys, io, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

F = r"F:/haowancheng/OpenOrange/launcher/dist/xlua.dll_unpacked.bin"
d = open(F, "rb").read()

def read_cstr(off):
    e = d.index(b"\0", off)
    return d[off:e].decode("ascii", "replace")

def find_string_in(handler_rva, window=0x120):
    """在处理函数里找 lea rdx,[rip+X] 指向的字符串"""
    s = handler_rva
    e = min(len(d) - 7, s + window)
    out = []
    q = s
    while q < e:
        # 48 8D 15 <disp32>  = lea rdx,[rip+disp]
        if d[q] == 0x48 and d[q+1] == 0x8D and d[q+2] == 0x15:
            disp = struct.unpack_from("<i", d, q + 3)[0]
            tgt = (q + 7 + disp) & 0xFFFFFFFF
            if 0 <= tgt < len(d):
                # 目标必须是可打印 ASCII 串
                end = tgt
                while end < len(d) and 32 <= d[end] < 127 and end - tgt < 64:
                    end += 1
                if end > tgt and end < len(d) and d[end] == 0:
                    out.append((tgt, d[tgt:end].decode("ascii")))
        q += 1
    return out

table = [
    (1, 0x11FF40), (2, 0x1206C0), (3, 0x11FE30), (4, 0x11FFE0),
    (5, 0x120250), (6, 0x11FBB0), (7, 0x11FCD0), (8, 0x11FDF0),
    (9, 0x124300), (10, 0x121B50), (11, 0x11FEC0), (12, 0x121AB0),
    (13, 0x121A30), (14, 0x1219E0),
]

print("%-5s %-10s %s" % ("类型", "处理函数", "RPC 名字（从函数里的 lua_pushstring 提取）"))
for typ, rva in table:
    names = find_string_in(rva)
    # 过滤掉明显不是 RPC 名的
    cand = [n for _, n in names if len(n) > 3]
    print("%-5d 0x%08X %s" % (typ, rva, " | ".join(cand[:4]) if cand else "(未找到)"))