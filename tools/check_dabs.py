#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""体检：扫描游戏所有 .dab 文件，找出文件头不是 UnityFS 的坏文件。

正常 Unity .dab 文件头是 "UnityFS"（55 6E 69 74 79 46 53）。
全零或其它头说明文件损坏/未下载完成。

用法
----
    # 扫默认安装
    python tools/check_dabs.py

    # 扫指定目录（比如新解压的整合包）
    python tools/check_dabs.py F:\haowancheng\GuiLongchao_new

    # 只要汇总，不逐个列
    python tools/check_dabs.py --quiet
"""
import argparse
import os
import sys

UNITYFS = b"UnityFS"
ZEROS = bytes(8)

DEFAULT_DIRS = [
    r"F:\haowancheng\GuiLongchao\game\GuiLongchao_Data\StreamingAssets\Assetbundle",
    r"F:\haowancheng\GuiLongchao\game\GuiLongchao_Data\PersistentPath\Patch\Assetbundle",
]


def scan_dir(d, quiet=False):
    total = 0
    good = 0
    bad = []
    for root, _dirs, files in os.walk(d):
        for fn in files:
            if not fn.lower().endswith(".dab"):
                continue
            p = os.path.join(root, fn)
            total += 1
            try:
                with open(p, "rb") as f:
                    head = f.read(8)
                size = os.path.getsize(p)
            except OSError as e:
                bad.append((p, "读失败: %s" % e))
                continue
            if head.startswith(UNITYFS):
                good += 1
            elif head == ZEROS:
                bad.append((p, "全零空壳 %d B" % size))
            else:
                bad.append((p, "未知头 %s %d B" % (head.hex(), size)))
    return total, good, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", help="要扫的目录；留空扫默认安装")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    args = ap.parse_args()

    targets = args.dirs or DEFAULT_DIRS
    grand_total = grand_good = 0
    all_bad = []
    for d in targets:
        if not os.path.isdir(d):
            print("跳过（不存在）: %s" % d)
            continue
        print("扫描 %s" % d)
        total, good, bad = scan_dir(d, args.quiet)
        print("  共 %d 个 .dab，正常 %d，异常 %d" % (total, good, len(bad)))
        grand_total += total
        grand_good += good
        all_bad.extend(bad)

    print("")
    print("=" * 60)
    print("合计: %d 个 .dab，正常 %d，异常 %d"
          % (grand_total, grand_good, len(all_bad)))
    if all_bad:
        print("")
        print("异常文件清单:")
        for p, why in all_bad:
            print("  %s" % p)
            print("     %s" % why)
        print("")
        print("修复：用整合包里的同名文件替换，或")
        print("      python tools/extract_patch_dabs.py <zip> --install")
    return 0 if not all_bad else 1


if __name__ == "__main__":
    sys.exit(main())
