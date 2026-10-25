#!/usr/bin/env python3
"""解析客户端的资源清单 ab_mapping.bin / big_pack_info.bin。

这两个文件在
    GuiLongchao_Data/StreamingAssets/Assetbundle/
    GuiLongchao_Data/PersistentPath/Patch/Assetbundle/
各有一份，是**明文**的（不像 assets_md5_cache.json 那样加密），
里面就是「资源路径 -> 资源包名」和「资源包 -> 大包」的映射。

ab_mapping.bin 布局（实测逆向）：
    u32  资源包总数
    u32  资源路径总数
    循环：
        u16 包名长度 + 包名（32 位十六进制 + ".bundle"）
        u16 资源数
        循环：u16 路径长度 + 路径
        尾部还有一段定长数据（u16 计数 + 若干 u32），
        本工具用「扫描到下一个合法包名」的方式跳过它，不解释其语义。

big_pack_info.bin 里全是 32 位十六进制名字（资源包名 + 大包名），
配套的 u32/u64 是包在大包里的偏移和大小。

用法:
    # 查某个资源路径在哪个包里
    python tools/parse_ab_mapping.py lookup mainstage

    # 判断某个场景名在资源清单里有没有对应的目录（客户端 IsAssetExist 就是这么判的）
    python tools/parse_ab_mapping.py scene sc_mainstage sc_favorspace login3

    # 统计两个清单的规模
    python tools/parse_ab_mapping.py stats
"""
import argparse
import os
import re
import struct
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GAME = os.environ.get("OO_GAME_DIR") or os.path.join(
    ROOT, "..", "GuiLongchao", "game", "GuiLongchao_Data")

SOURCES = [
    ("base", os.path.join(GAME, "StreamingAssets", "Assetbundle")),
    ("patch", os.path.join(GAME, "PersistentPath", "Patch", "Assetbundle")),
]

HEX32 = re.compile(rb"[0-9a-f]{32}")


def try_record(d, off):
    """若 off 处是合法记录，返回 (包名, [资源路径], 记录结束位置)，否则 None。"""
    n = len(d)
    if off + 4 > n:
        return None
    ln = struct.unpack_from("<H", d, off)[0]
    if ln < 8 or ln > 80:
        return None
    p = off + 2
    if p + ln > n:
        return None
    name = d[p:p + ln]
    if not name.endswith(b".bundle"):
        return None
    p += ln
    if p + 2 > n:
        return None
    cnt = struct.unpack_from("<H", d, p)[0]
    if cnt > 64:
        return None
    p += 2
    paths = []
    for _ in range(cnt):
        if p + 2 > n:
            return None
        pl = struct.unpack_from("<H", d, p)[0]
        p += 2
        if pl > 1024 or p + pl > n:
            return None
        paths.append(d[p:p + pl].decode("utf-8", "replace"))
        p += pl
    return name.decode("utf-8", "replace"), paths, p


def parse_ab_mapping(path):
    """解析 ab_mapping.bin，返回 {包名: [资源路径]}。"""
    d = open(path, "rb").read()
    bundles = {}
    pos = 8
    while pos + 4 <= len(d):
        rec = try_record(d, pos)
        if rec:
            name, paths, end = rec
            bundles.setdefault(name, []).extend(paths)
            pos = end
            continue
        pos += 1
    return bundles


def parse_big_pack(path):
    """big_pack_info.bin 里所有 32 位十六进制名字（包名 + 大包名）。"""
    d = open(path, "rb").read()
    return sorted({m.group().decode() for m in HEX32.finditer(d)})


def load(tag):
    for t, d in SOURCES:
        if t != tag:
            continue
        ab = os.path.join(d, "ab_mapping.bin")
        bp = os.path.join(d, "big_pack_info.bin")
        bundles = parse_ab_mapping(ab) if os.path.exists(ab) else {}
        packs = parse_big_pack(bp) if os.path.exists(bp) else []
        return bundles, packs
    raise SystemExit("未知清单: {}".format(tag))


def cmd_stats(_argv):
    for tag, d in SOURCES:
        bundles, packs = load(tag)
        n_paths = sum(len(v) for v in bundles.values())
        dabs = [f for f in os.listdir(d) if f.lower().endswith(".dab")] if os.path.isdir(d) else []
        print("== {} ({})".format(tag, d))
        print("   资源包 %d 个，资源路径 %d 条" % (len(bundles), n_paths))
        print("   big_pack_info 名字 %d 个（含包名与大包名）" % len(packs))
        print("   盘上 .dab 大包 %d 个" % len(dabs))


def cmd_lookup(argv):
    if not argv:
        raise SystemExit("用法: parse_ab_mapping.py lookup <关键字> [关键字 ...]")
    for tag, _d in SOURCES:
        bundles, _packs = load(tag)
        print("== {}".format(tag))
        for kw in argv:
            hits = [(b, a) for b, ps in bundles.items() for a in ps if kw.lower() in a.lower()]
            print("   关键字 %-20s 命中 %d" % (kw, len(hits)))
            for b, a in hits[:10]:
                print("      {}  <-  {}".format(b, a))


def cmd_scene(argv):
    """判断场景名在清单里有没有资源目录。

    实测规律：场景名会作为目录出现在 scenes/ 下面，
      scenes/UI/login3/...        -> login3
      scenes/UI/sc_favorspace/... -> sc_favorspace
      scenes/Level/material/sc_material_6/... -> sc_material_6
    客户端 IsAssetExist(场景名) 的结果和这个判断完全一致（实测过
    login3/sc_favorspace/sc_material_6 为真，sc_mainstage/sc_Infinite_challenge 为假）。
    """
    if not argv:
        raise SystemExit("用法: parse_ab_mapping.py scene <场景名> [...]")
    bundles, _packs = load("patch")
    all_paths = [a for ps in bundles.values() for a in ps]
    print("清单：patch（%d 条资源路径）" % len(all_paths))
    for name in argv:
        seg = "/" + name + "/"
        hits = [a for a in all_paths if seg in ("/" + a)]
        print("   %-24s %s" % (name, "有资源目录（%d 条）" % len(hits) if hits else "★ 清单里没有"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stats", "lookup", "scene"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    {"stats": cmd_stats, "lookup": cmd_lookup, "scene": cmd_scene}[a.cmd](a.args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
