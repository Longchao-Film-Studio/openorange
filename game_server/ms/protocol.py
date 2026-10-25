#!/usr/bin/env python3
"""protobuf / msgpack 编解码（P0-E3 从 ms.py 拆出）。

两边都是"够用就好"的最小实现，不是通用库：
  protobuf  只支持 varint(field=1,2,3,5) 和 bytes(field=2,3,4,5)，
            这正是 ClientGate / GameLoadService 用到的全部形态。
  msgpack   只支持 map/array/str/int/bool/float，够表达 initDict 与 RPC 参数。

★ 实测踩过的坑：initDict 里 expireTime / getTime 是 double，
  msgpack 编码是 0xCB，早期没处理这个分支会退回 (None, i+1) 导致整条流错位。
"""
import struct


def pb_read_varint(data, i):
    shift = 0
    val = 0
    while True:
        if i >= len(data):
            raise ValueError("varint truncated")
        b = data[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")

def pb_parse(data):
    """返回 {field_no: [value, ...]}；varint 为 int，len-delimited 为 bytes。"""
    out = {}
    i = 0
    while i < len(data):
        tag = data[i]
        i += 1
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            v, i = pb_read_varint(data, i)
            out.setdefault(fn, []).append(v)
        elif wt == 2:
            ln, i = pb_read_varint(data, i)
            out.setdefault(fn, []).append(data[i:i + ln])
            i += ln
        elif wt == 5:
            out.setdefault(fn, []).append(data[i:i + 4])
            i += 4
        elif wt == 1:
            out.setdefault(fn, []).append(data[i:i + 8])
            i += 8
        else:
            raise ValueError(f'unsupported wiretype {wt} at field {fn}')
    return out

def pb_varint_field(fn, v):
    out = bytearray([(fn << 3) | 0])
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def pb_bytes_field(fn, b):
    out = bytearray([(fn << 3) | 2])
    ln = len(b)
    while True:
        x = ln & 0x7F
        ln >>= 7
        if ln:
            out.append(x | 0x80)
        else:
            out.append(x)
            break
    return bytes(out) + b

def mp_str(s):
    b = s.encode("utf-8")
    n = len(b)
    if n < 32:
        return bytes([0xA0 | n]) + b
    if n < 256:
        return b"\xd9" + bytes([n]) + b
    return b"\xda" + struct.pack(">H", n) + b

def mp_int(v):
    """msgpack 整数编码（P0-E3 修正：负数以前编码是坏的）。

    ★ 原来的负数分支是  b"\xff" + struct.pack(">q", v)  —— 多写了一个 0xff 前缀，
      于是 -1 变成 9 字节（8 个 0xff + 一个 0xff），客户端按负 fixint 只读 1 字节，
      剩下 8 字节被当成后续数据的开头，**整条 msgpack 流错位**。
      现在按 spec 分层编码：负 fixint / int8 / int16 / int32 / int64。
      （tests/test_ms_frames.py 的 msgpack 往返用例会守住这一点）
    """
    if v < 0:
        if v >= -32:
            return bytes([v & 0xFF])                  # negative fixint
        if v >= -128:
            return b"\xd0" + struct.pack(">b", v)     # int8
        if v >= -32768:
            return b"\xd1" + struct.pack(">h", v)     # int16
        if v >= -(1 << 31):
            return b"\xd2" + struct.pack(">i", v)     # int32
        return b"\xd3" + struct.pack(">q", v)         # int64
    if v < 128:
        return bytes([v])                             # positive fixint
    if v < 256:
        return b"\xcc" + bytes([v])                   # uint8
    if v < 65536:
        return b"\xcd" + struct.pack(">H", v)         # uint16
    if v < (1 << 32):
        return b"\xce" + struct.pack(">I", v)         # uint32
    return b"\xcf" + struct.pack(">Q", v)             # uint64

def mp_bool(v):
    return b"\xc3" if v else b"\xc2"

def mp_float(v):
    """msgpack float64（0xCB）。"""
    return b"\xcb" + struct.pack(">d", float(v))

def mp_any(v):
    if v is None:
        # msgpack nil（0xC0）。UseItem_CS 的 ext 参数经常是 nil。
        return b"\xc0"
    if isinstance(v, float):
        return mp_float(v)
    if isinstance(v, bool):
        return mp_bool(v)
    if isinstance(v, int):
        return mp_int(v)
    if isinstance(v, str):
        return mp_str(v)
    if isinstance(v, list):
        return mp_arr(v)
    if isinstance(v, dict):
        return mp_map(v)
    raise TypeError(f"unsupported msgpack value: {v!r}")

def mp_map(d):
    """msgpack map。

    ★ 超过 15 项必须用 map16(0xDE)，不能再用 0x80|n ——
    0x80|173 = 0xAD 是 fixstr(13) 的标记，客户端会把整张表当字符串解，
    报 "bad argument #1 to 'pairs' (table expected, got string)"。
    """
    n = len(d)
    if n < 16:
        out = bytearray([0x80 | n])
    else:
        out = bytearray(b"\xde" + struct.pack(">H", n))
    for k, v in d.items():
        # 键也要走 mp_any：自定义字典里 __IntTypeKey__=true 的类型（如
        # BooleanDictKeyInt）键必须是 msgpack 整数。见 conv_int_keys。
        out += mp_str(k) if isinstance(k, str) else mp_any(k)
        out += mp_any(v)
    return bytes(out)

def mp_arr(items):
    """msgpack array。同样：超过 15 项要用 array16(0xDC)。"""
    n = len(items)
    if n < 16:
        out = bytearray([0x90 | n])
    else:
        out = bytearray(b"\xdc" + struct.pack(">H", n))
    for v in items:
        out += mp_any(v)
    return bytes(out)

def mp_decode(b, i=0):  # noqa: C901,PLR0911,PLR0912  — msgpack 解码器：分支与 return 直接对应 wire type 规范（每类型一个分支），拆分只会让状态机更难读
    """最小 msgpack 解码器，够用就行。返回 (值, 新下标)。"""
    if i >= len(b):
        return None, i
    c = b[i]; i += 1
    if c <= 0x7F:
        return c, i
    if c >= 0xE0:
        return c - 0x100, i
    if 0x80 <= c <= 0x8F:                      # fixmap
        n = c & 0x0F
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if 0x90 <= c <= 0x9F:                      # fixarray
        n = c & 0x0F
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if 0xA0 <= c <= 0xBF:                      # fixstr
        n = c & 0x1F
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xCA:                              # float32
        return struct.unpack_from(">f", b, i)[0], i + 4
    if c == 0xCB:                              # float64
        # initDict 里 expireTime / getTime 是 double，编码就是 0xCB。
        # 以前没处理这个分支，会退回 (None, i+1) 导致整条流错位。
        return struct.unpack_from(">d", b, i)[0], i + 8
    if c == 0xCC:
        return b[i], i + 1
    if c == 0xCD:
        return int.from_bytes(b[i:i + 2], "big"), i + 2
    if c == 0xCE:
        return int.from_bytes(b[i:i + 4], "big"), i + 4
    if c == 0xCF:
        return int.from_bytes(b[i:i + 8], "big"), i + 8
    if c == 0xD0:
        return int.from_bytes(b[i:i + 1], "big", signed=True), i + 1
    if c == 0xD1:
        return int.from_bytes(b[i:i + 2], "big", signed=True), i + 2
    if c == 0xD2:
        return int.from_bytes(b[i:i + 4], "big", signed=True), i + 4
    if c == 0xD3:
        return int.from_bytes(b[i:i + 8], "big", signed=True), i + 8
    if c == 0xD9:
        n = b[i]; i += 1
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDA:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDB:                              # str32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xDC:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if c == 0xDD:                              # array32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        arr = []
        for _ in range(n):
            v, i = mp_decode(b, i)
            arr.append(v)
        return arr, i
    if c == 0xDE:
        n = int.from_bytes(b[i:i + 2], "big"); i += 2
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if c == 0xDF:                              # map32
        n = int.from_bytes(b[i:i + 4], "big"); i += 4
        d = {}
        for _ in range(n):
            k, i = mp_decode(b, i)
            v, i = mp_decode(b, i)
            d[k] = v
        return d, i
    if c in (0xC0,):
        return None, i
    if c in (0xC2, 0xC3):
        return c == 0xC3, i
    return None, i


def conv_int_keys(obj):
    """把 JSON 里 "#<int>" 形式的键转成真正的整数键。

    JSON 的对象键只能是字符串，但客户端自定义字典（CustomDict:roinit）
    会把键原样放进 _properties，不做类型转换；__IntTypeKey__=true 的类型
    必须收到整数键。所以生成器里用 "#1" 这种写法表示整数 1。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("#") and k[1:].lstrip("-").isdigit():
                out[int(k[1:])] = conv_int_keys(v)
            else:
                out[k] = conv_int_keys(v)
        return out
    if isinstance(obj, list):
        return [conv_int_keys(v) for v in obj]
    return obj
