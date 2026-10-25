"""从已加载的 xlua.dll 内存镜像里 dump 解密后的 PE。

背景：xlua.dll 是加壳的 —— 文件里 .text/.rdata/.data 的 raw size 全是 0，
真实代码只有在 LoadLibrary 之后才被解出来。所以静态反编译拿不到东西，
必须先把内存里的镜像 dump 出来。

用法: python dump_xlua.py <xlua.dll 路径> <输出 .bin>
"""
import ctypes
import struct
import sys
from ctypes import wintypes

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.LoadLibraryW.restype = wintypes.HMODULE
k32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]


def main():
    if len(sys.argv) < 3:
        sys.exit("用法: dump_xlua.py <dll> <out.bin>")
    dll, outp = sys.argv[1], sys.argv[2]
    h = k32.LoadLibraryW(dll)
    if not h:
        sys.exit(f'LoadLibrary 失败: {ctypes.get_last_error()}')
    print(f"模块基址: {h:#x}")

    base = h
    hdr = ctypes.string_at(base, 0x1000)
    e_lfanew = struct.unpack_from('<I', hdr, 0x3C)[0]
    assert hdr[e_lfanew:e_lfanew+4] == b'PE\x00\x00', '不是 PE'
    coff = e_lfanew + 4
    nsec, = struct.unpack_from('<H', hdr, coff + 2)
    opt_size, = struct.unpack_from('<H', hdr, coff + 16)
    opt = coff + 20
    _magic, = struct.unpack_from('<H', hdr, opt)
    size_of_image, = struct.unpack_from('<I', hdr, opt + 56)
    print(f'sections={nsec:d}  SizeOfImage={size_of_image:#x} ({size_of_image // 1048576:d} MB)')

    img = ctypes.string_at(base, size_of_image)
    with open(outp, 'wb') as f:
        f.write(img)
    print(f'已 dump {len(img)} 字节 -> {outp}')

    # 校验：导出地址处应该是正常 x64 代码（luaopen_* 通常是 jmp/call 开头）
    sect = opt + opt_size
    secs = []
    for i in range(nsec):
        o = sect + i * 40
        nm = hdr[o:o+8].rstrip(b'\x00').decode('ascii', 'replace')
        vs, va = struct.unpack_from('<II', hdr, o + 8)
        secs.append((nm, va, vs))
    print("\n段（内存里）:")
    for nm, va, vs in secs:
        print(f'   {nm:<10} va={va:0#10x} vs={vs:#x}')
    for rva, label in ((0x152a3, 'luaopen_phonestcore'), (0x87a6, 'luaL_newstate')):
        print('  {:<22} rva={:#x} bytes={}'.format(label, rva, img[rva:rva+24].hex(' ')))


if __name__ == "__main__":
    main()