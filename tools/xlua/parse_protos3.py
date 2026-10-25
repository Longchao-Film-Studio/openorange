
"""完整解析 proto 描述符：service / method / message / field。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
P = r"F:\ghidra\out\xlua_unpacked.dll"
OUT = r"F:\haowancheng\OpenOrange\docs\proto协议.md"
d = open(P, "rb").read()

TYPES = {1:"double",2:"float",3:"int64",4:"uint64",5:"int32",6:"fixed64",7:"fixed32",
         8:"bool",9:"string",10:"group",11:"message",12:"bytes",13:"uint32",14:"enum",
         15:"sfixed32",16:"sfixed64",17:"sint32",18:"sint64"}
LABELS = {1:"optional",2:"required",3:"repeated"}

def rv(buf, i):
    r = 0; s = 0
    while True:
        b = buf[i]; i += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): return r, i
        s += 7

def fields(buf):
    out = []; i = 0; n = len(buf)
    while i < n:
        try: key, i = rv(buf, i)
        except Exception: break
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rv(buf, i); out.append((f, 0, v))
        elif wt == 2:
            ln, i = rv(buf, i); out.append((f, 2, buf[i:i+ln])); i += ln
        elif wt == 5: out.append((f, 5, buf[i:i+4])); i += 4
        elif wt == 1: out.append((f, 1, buf[i:i+8])); i += 8
        else: break
    return out

def find_descriptor(name):
    # 名字可能在别的 descriptor 里作为 import 出现，所以要扫所有出现位置，
    # 只接受「前面正好是 0x0A + 长度 = len(name)」的那个。
    starts = []
    pos = 0
    while True:
        off = d.find(name, pos)
        if off < 0: break
        pos = off + 1
        for back in range(1, 6):
            s2 = off - back
            if s2 < 0: continue
            try:
                l2, i2 = rv(d, s2)
            except Exception:
                continue
            if l2 == len(name) and i2 == off:
                starts.append(s2)
                break
    if not starts:
        return None
    # 取最长的那个（真正的 file descriptor 一定比 import 引用长）
    best = None
    for start in starts:
        blob = _scan(start)
        if blob is not None and (best is None or len(blob) > len(best)):
            best = blob
    return best


def _scan(start):
    if start is None:
        return None
    cur = start; end = start
    while cur < len(d) - 4:
        try: key, ni = rv(d, cur)
        except Exception: break
        f, wt = key >> 3, key & 7
        if f == 0 or f > 20 or wt not in (0,1,2,5): break
        try:
            if wt == 0: _, cur = rv(d, ni)
            elif wt == 2:
                l, ni2 = rv(d, ni)
                if ni2 + l > len(d): break
                cur = ni2 + l
            elif wt == 5: cur = ni + 4
            else: cur = ni + 8
        except Exception: break
        end = cur
    return d[start:end]

def dump_message(blob, lines):
    fs = fields(blob)
    mname = b"?"
    for f, wt, v in fs:
        if f == 1 and wt == 2: mname = v
    lines.append("**%s**" % mname.decode())
    lines.append("")
    for f, wt, v in fs:
        if f != 2 or wt != 2: continue
        ff = fields(v)
        nm = num = typ = None; tname = None; lab = 1
        for a, w2, b in ff:
            if a == 1 and w2 == 2: nm = b.decode()
            elif a == 3 and w2 == 0: num = b
            elif a == 4 and w2 == 0: lab = b
            elif a == 5 and w2 == 0: typ = b
            elif a == 6 and w2 == 2: tname = b.decode()
        ts = TYPES.get(typ, str(typ))
        if tname: ts = tname
        lines.append("- %s = %s : %s %s" % (num, nm, LABELS.get(lab, ""), ts))
    lines.append("")

lines = []
lines.append("# 归橙 · 传输层 proto 协议（静态提取）")
lines.append("")
lines.append("由 work/oo_analysis/parse_protos3.py 从解密后的 xlua.dll 镜像里提取。")
lines.append("")
lines.append("数据来源：.rdata 里序列化的 FileDescriptorProto（protobuf 自带描述符）。")
lines.append("和之前内存扫描得到的服务方法表完全一致。")
lines.append("")

for fname in (b"client_gate.proto", b"ms_gate.proto", b"phonest.proto", b"ms_common.proto"):
    blob = find_descriptor(fname)
    if not blob or len(blob) < 10:
        continue
    lines.append("## " + fname.decode())
    lines.append("")
    for f, wt, v in fields(blob):
        if f == 2 and wt == 2:
            lines.append("package: " + v.decode())
            lines.append("")
        elif f == 3 and wt == 2:
            lines.append("- import " + v.decode())
        elif f == 4 and wt == 2:
            lines.append("### message")
            lines.append("")
            dump_message(v, lines)
        elif f == 6 and wt == 2:
            sf = fields(v)
            sname = b"?"
            for a, w2, b in sf:
                if a == 1 and w2 == 2: sname = b
            lines.append("### service " + sname.decode())
            lines.append("")
            lines.append("| 方法 | 请求 | 响应 |")
            lines.append("|---|---|---|")
            for a, w2, b in sf:
                if a == 2 and w2 == 2:
                    mf = fields(b); nm = inp = outp = b""
                    for c, w3, e in mf:
                        if c == 1 and w3 == 2: nm = e
                        elif c == 2 and w3 == 2: inp = e
                        elif c == 3 and w3 == 2: outp = e
                    lines.append("| " + nm.decode() + " | " + inp.decode() + " | " + outp.decode() + " |")
            lines.append("")

open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("已写出 %s（%d 行）" % (OUT, len(lines)))
