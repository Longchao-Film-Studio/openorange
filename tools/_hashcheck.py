# -*- coding: utf-8 -*-
import zlib, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

known = {
  "connectResponse": 2119322371,
  "sendConnectServer": 2087431183,
  "sendRequestEncryptToken": 1378977164,
  "confirmEncryptKeyAck": 543775382,
  "sendHeartbeat": 1574166541,
  "bindAccount": 889905872,
}

def fnv1a32(s):
    h = 0x811c9dc5
    for b in s.encode():
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h

def fnv1_32(s):
    h = 0x811c9dc5
    for b in s.encode():
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    return h

def djb2(s):
    h = 5381
    for b in s.encode():
        h = ((h * 33) + b) & 0xFFFFFFFF
    return h

def djb2_xor(s):
    h = 5381
    for b in s.encode():
        h = ((h * 33) ^ b) & 0xFFFFFFFF
    return h

def sdbm(s):
    h = 0
    for b in s.encode():
        h = (b + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
    return h

algos = {
  "crc32": lambda s: zlib.crc32(s.encode()) & 0xFFFFFFFF,
  "fnv1a32": fnv1a32,
  "fnv1_32": fnv1_32,
  "djb2": djb2,
  "djb2_xor": djb2_xor,
  "sdbm": sdbm,
}

for name, expect in known.items():
    print("%-26s expect=%u (0x%08X)" % (name, expect, expect))
    for an, fn in algos.items():
        v = fn(name)
        mark = "  <<< MATCH" if v == expect else ""
        print("    %-10s %u (0x%08X)%s" % (an, v, v, mark))
    print()