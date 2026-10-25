#!/usr/bin/env python3
"""解包归龙潮的 UnityFS AssetBundle（UnityCN 归龙潮变体）。

算法来源：AXiX-official/Studio（AssetStudio 分支）
  AssetStudio/Crypto/UnityCNGuiLongChao.cs
  AssetStudio/BundleFile.cs  (ReadUnityCN / ReadBlocks / ReadBlocksInfoAndDirectory)
  AssetStudio/Extensions/ByteArrayExtensions.cs (ToUInt4Array)

要点：归龙潮**不用 AES**。包头里有 8 字节，展开成 16 个半字节组成替表 Sub，
之后用「半字节轮转」流密码解密每个数据块。不需要任何密钥。
"""
import os
import struct
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import lz4.block as lz4block
except Exception:
    lz4block = None

FLAG_COMPRESSION_MASK = 0x3F
FLAG_BLOCKS_AT_END = 0x80
FLAG_BLOCKINFO_PADDING = 0x200
FLAG_UNITYCN = 0x400
FLAG_UNITYCN_NEW = 0x1000

COMP_NONE, COMP_LZMA, COMP_LZ4, COMP_LZ4HC = 0, 1, 2, 3


def read_cstr(data, p):
    e = data.index(b"\x00", p)
    return data[p:e].decode("utf-8", "replace"), e + 1


def align(p, n):
    return (p + n - 1) // n * n


def rotl8(x, n):
    n &= 7
    if n == 0:
        return x
    return ((x << n) | (x >> (8 - n))) & 0xFF


def build_sub(info8):
    """8 字节 -> 16 个半字节 -> 转置成 Sub[16]"""
    nibbles = []
    for b in info8:
        nibbles.append(b >> 4)
        nibbles.append(b & 0xF)
    sub = [0] * 16
    for i in range(16):
        idx = (i % 4 * 4) + (i // 4)
        sub[idx] = nibbles[i]
    return sub


def decrypt_byte(buf, base, off, index, sub):
    b = (sub[((index >> 2) & 3) + 4] + sub[index & 3]
         + sub[((index >> 4) & 3) + 8] + sub[((index & 0xFF) >> 6) + 12])
    pos = base + off
    if 0 <= pos < len(buf):
        buf[pos] = rotl8(buf[pos], b & 7)
        v = buf[pos]
    else:
        v = 0
    return v, off + 1, index + 1


def decrypt_segment(buf, base, index, remaining, sub):
    off = 0
    cur, off, index = decrypt_byte(buf, base, off, index, sub)
    byte_high = cur >> 4
    byte_low = cur & 0xF
    if byte_high == 0xF:
        while True:
            b, off, index = decrypt_byte(buf, base, off, index, sub)
            byte_high += b
            if b != 0xFF:
                break
    off += byte_high
    if off < remaining:
        _, off, index = decrypt_byte(buf, base, off, index, sub)
        _, off, index = decrypt_byte(buf, base, off, index, sub)
        if byte_low == 0xF:
            while True:
                b, off, index = decrypt_byte(buf, base, off, index, sub)
                if b != 0xFF:
                    break
    return off


def decrypt_block(buf, size, start_index, sub):
    count = 0
    offset = 0
    index = start_index
    while offset < size:
        if count >= 0x14:
            break
        count += 1
        offset += decrypt_segment(buf, offset, index, size - offset, sub)
        index += 1
    return buf


def decompress(data, comp, uncompressed_size):
    if comp == COMP_NONE:
        return data
    if comp in (COMP_LZ4, COMP_LZ4HC):
        if lz4block is None:
            raise RuntimeError("需要 lz4 模块：pip install lz4")
        return lz4block.decompress(data, uncompressed_size=uncompressed_size)
    if comp == COMP_LZMA:
        import lzma
        # Unity 的 LZMA 有 5 字节属性 + 8 字节大小头，这里只做兜底
        return lzma.decompress(data)
    raise RuntimeError(f'不支持的压缩类型 {comp}')


def parse_bundle(data):
    p = 0
    sig, p = read_cstr(data, p)
    if sig not in ("UnityFS", "ENCR"):
        raise RuntimeError(f"不是 UnityFS 包: {sig!r}")
    version = struct.unpack_from(">I", data, p)[0]; p += 4
    unity_ver, p = read_cstr(data, p)
    unity_rev, p = read_cstr(data, p)
    total_size = struct.unpack_from(">q", data, p)[0]; p += 8
    ci_size = struct.unpack_from(">I", data, p)[0]; p += 4
    ui_size = struct.unpack_from(">I", data, p)[0]; p += 4
    flags = struct.unpack_from(">I", data, p)[0]; p += 4

    info = {
        "signature": sig, "version": version, "unity_version": unity_ver,
        "unity_revision": unity_rev, "size": total_size,
        "compressed_blocks_info_size": ci_size,
        "uncompressed_blocks_info_size": ui_size,
        "flags": flags,
    }

    sub = None
    has_padding = True
    if flags & (FLAG_UNITYCN | FLAG_UNITYCN_NEW):
        p += 4                      # ReadUInt32
        info8 = data[p:p + 8]; p += 8
        p = align(p, 4)             # AlignStream()
        sub = build_sub(info8)
        info["sub"] = sub
        info["encrypted"] = True
    else:
        info["encrypted"] = False

    # ---- blocksInfo
    if version >= 7:
        p = align(p, 16)
    if flags & FLAG_BLOCKS_AT_END:
        pos = p
        p = total_size - ci_size
        bi_raw = data[p:p + ci_size]
        p = pos
    else:
        bi_raw = data[p:p + ci_size]; p += ci_size

    comp = flags & FLAG_COMPRESSION_MASK
    bi = decompress(bi_raw, comp, ui_size)

    # blocksInfo 里的整数是**大端序**（Unity 的 ArchiveFileSystem 就是这么写的，
    # AssetStudio 的 EndianBinaryReader 默认也是 BigEndian）
    q = 0
    bi[q:q + 16]; q += 16
    nblocks = struct.unpack_from(">I", bi, q)[0]; q += 4
    blocks = []
    for _ in range(nblocks):
        us = struct.unpack_from(">I", bi, q)[0]; q += 4
        cs = struct.unpack_from(">I", bi, q)[0]; q += 4
        bf = struct.unpack_from(">H", bi, q)[0]; q += 2
        blocks.append((us, cs, bf))
    nnodes = struct.unpack_from(">I", bi, q)[0]; q += 4
    nodes = []
    for _ in range(nnodes):
        off = struct.unpack_from(">q", bi, q)[0]; q += 8
        sz = struct.unpack_from(">q", bi, q)[0]; q += 8
        fl = struct.unpack_from(">I", bi, q)[0]; q += 4
        name, q = read_cstr(bi, q)
        nodes.append({"offset": off, "size": sz, "flags": fl, "path": name})

    if has_padding and (flags & FLAG_BLOCKINFO_PADDING):
        p = align(p, 16)

    # ---- 数据块
    stream = bytearray()
    for i, (us, cs, bf) in enumerate(blocks):
        chunk = bytearray(data[p:p + cs]); p += cs
        if (bf & 0x100) and sub is not None:
            decrypt_block(chunk, cs, i, sub)
        bcomp = bf & FLAG_COMPRESSION_MASK
        stream.extend(decompress(bytes(chunk), bcomp, us))

    info["blocks"] = blocks
    info["nodes"] = nodes
    info["stream"] = bytes(stream)
    return info


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"F:\haowancheng\GuiLongchao\game\GuiLongchao_Data\PersistentPath\Patch\Assetbundle\ab459e9ff483872f78f65795f0b8f327.dab"
    data = open(path, "rb").read()
    print("文件:", os.path.basename(path), len(data), "字节")
    info = parse_bundle(data)
    print("Unity 版本:", info["unity_revision"], " flags=0x{:X}".format(info["flags"]))
    print("加密:", info["encrypted"], " Sub:", info.get("sub"))
    print("块数:", len(info["blocks"]), " 节点数:", len(info["nodes"]))
    print("解压后数据流:", len(info["stream"]), "字节")
    print()
    print("=== 前 20 个节点 ===")
    for n in info["nodes"][:20]:
        print('  {:<60} off={:<10d} size={:d}'.format(n["path"], n["offset"], n["size"]))
