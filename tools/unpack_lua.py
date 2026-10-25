#!/usr/bin/env python3
"""从客户端 Patch/Lua 的全部 .bytes 包里解出所有 Lua 文件。

完全复刻 FPArchive.LuaArchiveFile.LoadAllArchiveFile 的逻辑：
  1. 读 LuaPackInfo.json（数组，顺序即加载顺序）
  2. 依次打开每个 .bytes，解析头部与索引
  3. 同名文件「先到先得」（后来的只记日志、不覆盖）—— 与客户端行为一致
  4. 按索引里的 pos/len 定位，XOR 解密后写盘

用法:
    python tools/unpack_lua.py --pack-dir <Patch/Lua 路径> --out <输出目录>
"""
import argparse
import json
import os
import struct
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KEY1 = bytes([26, 84, 94, 40, 155, 43, 205, 239, 226, 164, 86, 120, 144, 59, 207, 175])
TEXT_KEY = "hasdfeg@#$%9892^&^".encode("utf-8")
POS_BIAS = 356

try:
    import numpy as _np
    _KEY_NP = _np.frombuffer(KEY1, dtype=_np.uint8)
except Exception:
    _np = None


def xor_body(buf):
    """与 _key1 循环异或。用 numpy 加速（几万个文件，纯 Python 循环太慢）。"""
    n = len(buf)
    if n == 0:
        return b""
    if _np is not None:
        arr = _np.frombuffer(buf, dtype=_np.uint8)
        pad = _np.resize(_KEY_NP, n)
        return (_np.bitwise_xor(arr, pad)).tobytes()
    reps = (n // 16) + 1
    pad = (KEY1 * reps)[:n]
    return bytes(a ^ b for a, b in zip(buf, pad))


def xor_name(buf):
    kb = TEXT_KEY
    kl = len(kb)
    return bytes(b ^ kb[i % kl] for i, b in enumerate(buf))


def parse_pack(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 32:
        return None
    p = 0
    vlen = struct.unpack_from("<i", data, p)[0]; p += 4
    if vlen < 0 or vlen > 64 or p + vlen > len(data):
        return None
    version = data[p:p + vlen].decode("utf-8", "replace"); p += vlen
    file_count, body_size = struct.unpack_from("<ii", data, p); p += 8
    zip_flag = struct.unpack_from("<H", data, p)[0]; p += 2
    info_len = struct.unpack_from("<i", data, p)[0]; p += 4
    if info_len < 0 or p + info_len > len(data):
        return None
    info = data[p:p + info_len]

    q = 0
    num = struct.unpack_from("<i", info, q)[0]; q += 4
    if num < 0 or num > 200000:
        return None
    entries = []
    for _ in range(num):
        if q + 4 > len(info):
            break
        nlen = struct.unpack_from("<i", info, q)[0]; q += 4
        if nlen < 0 or q + nlen + 12 > len(info):
            break
        name = xor_name(info[q:q + nlen]).decode("utf-8", "replace"); q += nlen
        pos = struct.unpack_from("<q", info, q)[0] - POS_BIAS; q += 8
        ln = struct.unpack_from("<i", info, q)[0] // 2; q += 4
        entries.append((name, pos, ln))

    start_pos = struct.unpack_from("<q", data, len(data) - 8)[0]
    return {
        "version": version,
        "file_count": file_count,
        "zip_flag": zip_flag,
        "start_pos": start_pos,
        "entries": entries,
        "data": data,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="只处理名字含该子串的包")
    args = ap.parse_args()

    pack_dir = os.path.abspath(args.pack_dir)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    info_path = os.path.join(pack_dir, "LuaPackInfo.json")
    names = []
    if os.path.isfile(info_path):
        with open(info_path, "r", encoding="utf-8", errors="replace") as f:
            pack_list = json.load(f)
        names = [it.get("name") for it in pack_list if it.get("name")]

    # LuaPackInfo.json 只列了增量分片，基础包（TableData.bytes / Configs.bytes / UI.bytes …）
    # 不在清单里。所以清单之后再把目录里剩下的 .bytes 全部补上。
    listed = set(names)
    extra = sorted(f for f in os.listdir(pack_dir)
                   if f.endswith(".bytes") and f not in listed)
    names = names + extra
    print(f'LuaPackInfo.json 条目: {len(listed)}，目录补充: {len(extra)}，合计: {len(names)}')
    if extra:
        print("  补充的包:", ", ".join(extra))

    seen = {}
    pack_stats = []
    for idx, nm in enumerate(names):
        if not nm.endswith(".bytes"):
            continue
        if args.only and args.only.lower() not in nm.lower():
            continue
        full = os.path.join(pack_dir, nm)
        if not os.path.isfile(full):
            continue
        pk = parse_pack(full)
        if pk is None:
            print("  [跳过] 解析失败:", nm)
            continue
        added = 0
        dup = 0
        for name, pos, ln in pk["entries"]:
            key = name.lower()
            if key in seen:
                dup += 1
                continue
            off = pk["start_pos"] + pos
            if off < 0 or off + ln > len(pk["data"]):
                continue
            seen[key] = (nm, off, ln, pk["data"])
            added += 1
        pack_stats.append((nm, len(pk["entries"]), added, dup))
        if idx % 20 == 0 or added:
            print('  [{:3d}/{:d}] {:<34} 条目{:<6d} 新增{:<6d} 重复{:d}'.format(idx + 1, len(names), nm, len(pk["entries"]), added, dup))

    print()
    print("唯一文件数:", len(seen))
    print("开始写盘...")
    written = 0
    for key, (pack_name, off, ln, data) in seen.items():
        body = xor_body(data[off:off + ln])
        rel = key.replace("\\", "/")
        dst = os.path.join(out_dir, rel + ".luac")
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(body)
        written += 1
        if written % 5000 == 0:
            print(f'  已写 {written}')

    print("完成，写出", written, "个文件 ->", out_dir)


if __name__ == "__main__":
    sys.exit(main())
