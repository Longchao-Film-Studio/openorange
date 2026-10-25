#!/usr/bin/env python3
r"""从 PC 整合包 zip 里精确取出缺失的 .dab 补丁文件。

背景
----
PersistentPath/Patch/Assetbundle/ 里有 4 个 .dab 是「全零空壳」
（文件大小正常但内容全 0），导致 Battle_Main_Panel 的依赖 bundle
加载失败、战斗 HUD 缺失。

正常 .dab 的文件头是 UnityFS（55 6E 69 74 79 46 53），那 4 个是 00。

用法
----
    python tools/extract_patch_dabs.py D:\guanfu-pc.zip --dry-run
    python tools/extract_patch_dabs.py D:\guanfu-pc.zip --out work\patch_dabs
    python tools/extract_patch_dabs.py D:\guanfu-pc.zip --install

zip 是随机访问格式，只解这几个条目不会解整包。
"""
import argparse
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORRUPT = [
    "323f49930180b7158ae61f66d4dc1202.dab",
    "455e128ef52a6e295fe9b46d8167b837.dab",
    "827a946cad432fc6f5b3f7ce97667040.dab",
    "8e5fe7b1857ad76be6ec859ac8b328bb.dab",
]

GAME_PATCH_DIR = os.path.join(
    "F:\\haowancheng\\GuiLongchao\\game\\GuiLongchao_Data",
    "PersistentPath", "Patch", "Assetbundle")

UNITYFS = b"UnityFS"
ZEROS = bytes(8)


def check_dab(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"读不到: {e}"
    if head.startswith(UNITYFS):
        return True, f'正常 (UnityFS, {size} B)'
    if head == ZEROS:
        return False, f'全零空壳 ({size} B)'
    return False, f'未知头 {head.hex()} ({size} B)'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--out", default=None)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.zip_path):
        print(f"zip 不存在: {args.zip_path}")
        return 2

    print(f"打开 {args.zip_path} ({os.path.getsize(args.zip_path) / 1024 ** 3:.2f} GB)")
    with zipfile.ZipFile(args.zip_path) as z:
        infos = z.infolist()
        print(f'zip 内条目总数: {len(infos)}')

        by_name = {}
        dab_count = 0
        for i in infos:
            base = os.path.basename(i.filename)
            if base.endswith(".dab"):
                dab_count += 1
                by_name.setdefault(base, i)
        print(f'其中 .dab 条目: {dab_count}')

        want = CORRUPT if not args.all else sorted(by_name.keys())
        found = []
        for name in want:
            info = by_name.get(name)
            if info is None:
                print(f"  [缺失] {name}")
                continue
            found.append((name, info))
            print(f'  [找到] {name}  {info.file_size} B  路径: {info.filename}')

        if not found:
            print()
            print("zip 里没有目标文件。可能这个包不含 Patch 资源，"
                  "或者路径/命名不同。")
            return 1

        if args.dry_run:
            print()
            print("--dry-run：只检查，未解压。")
            return 0

        out_dir = args.out or os.path.join(HERE, "work", "patch_dabs")
        os.makedirs(out_dir, exist_ok=True)
        print()
        print(f"解压到 {out_dir}")
        ok_files = []
        for name, info in found:
            dst = os.path.join(out_dir, name)
            with z.open(info) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            good, why = check_dab(dst)
            print(f"  {name} -> {why}")
            if good:
                ok_files.append(name)

        if not args.install:
            print()
            print("完成。要装进游戏加 --install，或手动拷到:")
            print(f"  {GAME_PATCH_DIR}")
            return 0

        if not ok_files:
            print()
            print("没有校验通过的文件，不安装。")
            return 1

        os.makedirs(GAME_PATCH_DIR, exist_ok=True)
        bak_dir = os.path.join(GAME_PATCH_DIR, "_corrupt_backup")
        os.makedirs(bak_dir, exist_ok=True)
        print()
        print(f"安装到 {GAME_PATCH_DIR}")
        for name in ok_files:
            dst = os.path.join(GAME_PATCH_DIR, name)
            if os.path.exists(dst):
                good, why = check_dab(dst)
                if not good:
                    shutil.move(dst, os.path.join(bak_dir, name))
                    print(f"  备份坏文件 {name} -> _corrupt_backup/")
            shutil.copy2(os.path.join(out_dir, name), dst)
            good, why = check_dab(dst)
            print(f"  装好 {name} -> {why}")
        print()
        print("完成。重启游戏验证 HUD。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
