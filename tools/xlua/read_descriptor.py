#!/usr/bin/env python3
"""从解密镜像里读 proto 描述符（测试用：让断言对着**描述符**，不对着手写常量）。

## 为什么需要它

2026-10-31 那次事故：MsPush 的字段号靠"声明顺序从 1 开始"推断，写成了 1..6，
真实是 2..7 —— 推送静默失效一个月没被发现，因为测试断言的也是同一份错误推断。

所以测试必须能独立拿到真相。这里从 F:/ghidra/out/xlua_unpacked.dll 的
FileDescriptorProto 里逐字节解出 message 的字段表。

## 用法

    from tools.xlua.read_descriptor import message_fields
    f = message_fields("ms_common.proto", "MsPush")   # {2:"service", 3:"method", ...}

镜像不存在时返回 None（调用方可跳过相关断言，而不是假装通过）。
"""
from __future__ import annotations

import os

#: 解密镜像（tools/xlua/dump_xlua.py + rebuild_pe.py 的产物）
IMAGE = os.environ.get("OO_XLUA_IMAGE") or r"F:\ghidra\out\xlua_unpacked.dll"

_CACHE: dict | None = None


def _rv(buf, i):
    r = 0; s = 0
    while True:
        b = buf[i]; i += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80):
            return r, i
        s += 7


def _fields(buf, start=0, end=None):
    """把一段 protobuf 解成 [(field_no, value)]（只支持 varint / len / 32 / 64 位）。"""
    out = []; i = start; n = len(buf) if end is None else end
    while i < n:
        key, i = _rv(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _rv(buf, i); out.append((f, v))
        elif wt == 2:
            ln, i = _rv(buf, i); out.append((f, buf[i:i + ln])); i += ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            raise ValueError(f"wiretype {wt}")
    return out


def _file_descriptor(name: str):
    """定位 name 对应的 FileDescriptorProto 起点。

    ★ 不用启发式猜边界（parse_protos3.py 的 _scan 在 ms_common.proto 上返回 0 字节，
      实测过）。这里要求精确匹配「0x0A + 长度 == len(name) + name」，
      只认这一种前缀 —— 那就是 FileDescriptorProto 的 field 1（name）。
    """
    global _CACHE  # noqa: PLW0603
    if _CACHE is not None:
        d = _CACHE["data"]
    else:
        with open(IMAGE, "rb") as f:
            d = f.read()
        _CACHE = {"data": d}
    raw = name.encode("ascii")
    best = None
    pos = 0
    while True:
        off = d.find(raw, pos)
        if off < 0:
            break
        pos = off + 1
        # 前缀必须是 0x0A(len) <name>：0x0A 是 FileDescriptorProto 的 field 1。
        # 长度 >= 16 时 length 占两字节，所以回看要覆盖 1..3 字节。
        for back in (2, 3):
            tag_pos = off - back
            if tag_pos < 0 or d[tag_pos] != 0x0A:
                continue
            try:
                ln, i2 = _rv(d, tag_pos + 1)
            except (IndexError, ValueError):
                continue
            if ln == len(raw) and i2 == off:
                # 同一份 proto 可能被别的文件 import 引用；取能连读最长的那份
                if best is None or _read_len(d, tag_pos) > _read_len(d, best[1]):
                    best = (d, tag_pos)
                break
    return best


def _read_len(d, start):
    """从 start 连读顶层字段，返回消耗的字节数（用来挑最长的候选）。"""
    try:
        return len(_read_file(d, start)) and _consumed(d, start)
    except (IndexError, ValueError):
        return 0


def _consumed(d, start):
    i = start
    while True:
        key, j = _rv(d, i)
        f, wt = key >> 3, key & 7
        if f == 0 or f > 12 or wt not in (0, 1, 2, 5):
            return i - start
        if wt == 2:
            ln, k = _rv(d, j)
            if k + ln > len(d):
                return i - start
            i = k + ln
        elif wt == 0:
            _, i = _rv(d, j)
        elif wt == 5:
            i = j + 4
        else:
            i = j + 8



def _read_file(d, start):
    """从 FileDescriptorProto 起点连读顶层字段（遇到非法字段号就停）。"""
    out = []; i = start
    while True:
        key, j = _rv(d, i)
        f, wt = key >> 3, key & 7
        if f == 0 or f > 12 or wt not in (0, 1, 2, 5):
            break
        if wt == 2:
            ln, k = _rv(d, j)
            if k + ln > len(d):
                break
            out.append((f, d[k:k + ln])); i = k + ln
        elif wt == 0:
            v, i = _rv(d, j); out.append((f, v))
        elif wt == 5:
            out.append((f, d[j:j + 4])); i = j + 4
        else:
            out.append((f, d[j:j + 8])); i = j + 8
    return out


def message_fields(file_name: str, message: str):
    """file_name 里 message 的字段表 {字段号: 字段名}；取不到返回 None。"""
    if not os.path.exists(IMAGE):
        return None
    found = _file_descriptor(file_name)
    if found is None:
        return None
    d, start = found
    for f, v in _read_file(d, start):
        if f != 4:
            continue
        mf = _fields(v)
        names = {}; nums = {}
        mname = None
        for a, b in mf:
            if a == 1 and isinstance(b, bytes):
                mname = b.decode("utf-8", "replace")
        if mname != message:
            continue
        for a, b in mf:
            if a != 2 or not isinstance(b, bytes):
                continue
            nm = num = None
            for c, e in _fields(b):
                if c == 1 and isinstance(e, bytes):
                    nm = e.decode("utf-8", "replace")
                elif c == 3 and isinstance(e, int):
                    num = e
            if nm is not None and num is not None:
                names[num] = nm
                nums[nm] = num
        return names
    return None


def _enum_from(blob, enum: str):
    """从一个 EnumDescriptorProto 里取 {名字: 编号}；名字不符返回 None。"""
    ef = _fields(blob)
    ename = None
    for a, b in ef:
        if a == 1 and isinstance(b, bytes):
            ename = b.decode("utf-8", "replace")
    if ename != enum:
        return None
    out = {}
    for a, b in ef:
        if a == 2 and not isinstance(b, bytes):
            continue
        if a != 2:
            continue
        nm = num = None
        for c, e in _fields(b):
            if c == 1 and isinstance(e, bytes):
                nm = e.decode("utf-8", "replace")
            elif c == 2 and isinstance(e, int):
                num = e
        if nm is not None and num is not None:
            out[nm] = num
    return out


def enum_values(file_name: str, enum: str, *, in_message: str | None = None):
    """file_name 里 enum 的 {名字: 编号}；取不到返回 None。

    ★ 这些枚举在内嵌在 message 里的（`message EnumG2C { enum Type {...} }`），
      所以要找的是 message 内 field 4（nested enum），不是文件顶层 field 5。
      传 in_message 可指定所在 message（默认两个位置都找）。
    """
    if not os.path.exists(IMAGE):
        return None
    found = _file_descriptor(file_name)
    if found is None:
        return None
    d, start = found
    for f, v in _read_file(d, start):
        if f == 4 and isinstance(v, bytes):
            mf = _fields(v)
            mname = None
            for a, b in mf:
                if a == 1 and isinstance(b, bytes):
                    mname = b.decode("utf-8", "replace")
            if in_message is not None and mname != in_message:
                continue
            for a, b in mf:
                if a != 4 or not isinstance(b, bytes):
                    continue
                got = _enum_from(b, enum)
                if got is not None:
                    return got
        if f != 5 or not isinstance(v, bytes):
            continue
        ef = _fields(v)
        ename = None
        for a, b in ef:
            if a == 1 and isinstance(b, bytes):
                ename = b.decode("utf-8", "replace")
        if ename != enum:
            continue
        out = {}
        for a, b in ef:
            if a != 2 or not isinstance(b, bytes):
                continue
            nm = num = None
            for c, e in _fields(b):
                if c == 1 and isinstance(e, bytes):
                    nm = e.decode("utf-8", "replace")
                elif c == 2 and isinstance(e, int):
                    num = e
            if nm is not None and num is not None:
                out[nm] = num
        return out
    return None


if __name__ == "__main__":
    for m in ("MsPush", "MsRequest", "MsResponse", "Heartbeat", "RpcContext"):
        print(m, "->", message_fields("ms_common.proto", m))
    print("EnumG2C ->", enum_values("ms_common.proto", "EnumG2C"))
