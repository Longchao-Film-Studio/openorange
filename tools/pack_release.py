#!/usr/bin/env python3
"""把服务端（含启动器）打成一个 zip：只打能直接跑起来的东西。

用法:
    python tools/pack_release.py
    python tools/pack_release.py --out D:\\dist\\oo.zip
    python tools/pack_release.py --list          # 只看会打什么，不压缩

## 排除什么 / 为什么

| 排除 | 原因 |
|---|---|
| launcher/target/       | Rust 编译缓存，981 MB |
| var/                   | 存档 / 日志 / 抓包，运行期产物 |
| shared/data.db*        | 账号库，含密码哈希与会话 token |
| .git/ .mypy_cache/ .ruff_cache/ __pycache__/ Python/_cache/ | 缓存 |
| launcher/dist/*.bak_*  | 旧 DLL 备份 |
| launcher/dist/*.log    | oo_internal.log 15 MB |
| launcher/dist/work/    | 启动器运行目录 |
| launcher_state.ini     | 上次登录的账号 |

## 保留什么 / 为什么

| 保留 | 原因 |
|---|---|
| launcher/dist/OO_Launcher.exe | 启动器本体 |
| launcher/dist/OO_Internal.dll | 注入的 DLL |
| launcher/dist/OpenOrange.ini  | 配置模板 |
| launcher/crates/              | Rust 源码（能重新编译） |
| （不打）Python/               | 本机专属安装，含绝对路径，换了机器就是坏的 |

shared/data.db 不打进去：首启时 db.init() 会自动建空库，拿到包的人自己注册账号。
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 目录前缀（相对仓库根）：命中即整段跳过
EXCLUDE_DIRS = (
    "launcher/target",
    "var",
    "build",
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "launcher/dist/work",
    "archivescript",
    "my_scripts",
    # 本机专属解释器安装（含绝对路径，换机器就是坏的）；包依赖系统 Python
    "Python",
)

#: 具体文件（相对仓库根）
EXCLUDE_FILES = (
    "shared/data.db",
    "shared/data.db-shm",
    "shared/data.db-wal",
    "shared/data.db.pre-uid-migration",
    "launcher/dist/launcher_state.ini",
)

#: 文件名模式（任意层级命中即排除）
EXCLUDE_PATTERNS = (
    "*.bak_*", "*.pdb", "*.exp", "*.lib", "*.pyc", "*.pyo",
    "*.unpacked", "*.pcapng", "*.tmp", "*.log",
)


def _norm(path: str) -> str:
    return path.replace(os.sep, "/")


def is_excluded(rel: str) -> bool:
    rel = _norm(rel)
    for d in EXCLUDE_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return True
    if rel in EXCLUDE_FILES:
        return True
    leaf = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(leaf, p) for p in EXCLUDE_PATTERNS)


def collect(root: str) -> tuple[list[tuple[str, int]], int, int]:
    """返回 ([(相对路径, 字节数)], 跳过数, 总字节)。"""
    picked: list[tuple[str, int]] = []
    skipped = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = _norm(os.path.relpath(dirpath, root))
        # 整目录剪枝，避免白走 launcher/target 这种 981MB 的树
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded(("" if rel_dir == "." else rel_dir + "/") + d)
        ]
        for name in filenames:
            rel = ("" if rel_dir == "." else rel_dir + "/") + name
            full = os.path.join(dirpath, name)
            if is_excluded(rel):
                skipped += 1
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                skipped += 1
                continue
            picked.append((rel, size))
            total += size
    picked.sort()
    return picked, skipped, total


def build(root: str, out: str, *, verbose: bool = True) -> dict:
    picked, skipped, total = collect(root)
    if verbose:
        print(f"仓库根: {root}")
        print(f"输出  : {out}")
        print(f"收录 {len(picked)} 个文件，{total / 1048576:.1f} MB（跳过 {skipped} 个）")

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if os.path.exists(out):
        os.remove(out)

    prefix = os.path.basename(root)
    t0 = time.time()
    manifest = build_readme(picked)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, _size in picked:
            zf.write(os.path.join(root, rel), arcname=f"{prefix}/{rel}")
        # 包内说明：拿到包的人先看这个
        zf.writestr(f"{prefix}/README_FIRST.txt", manifest)
    elapsed = time.time() - t0
    size = os.path.getsize(out)
    if verbose:
        print(f"完成：{out}（{size / 1048576:.1f} MB，用时 {elapsed:.1f}s）")
    return {"files": len(picked), "skipped": skipped, "bytes": total,
            "zip_bytes": size, "seconds": elapsed, "path": out}


def build_readme(picked: list[tuple[str, int]]) -> str:
    """生成包内的 README_FIRST.txt。

    ★ 为什么要生成而不是手写一份塞进仓库：
      手写的那份会和实际打包内容漂移（排除了什么、留了什么，两处各写一遍）。
      这里直接从本次真实收录清单取数字，包里的说明永远和包一致。
    """
    has = {rel for rel, _ in picked}

    def mark(rel: str) -> str:
        return "有" if rel in has else "缺"

    lines = [
        "OpenOrange 服务端",
        "=" * 60,
        "",
        "## 一、你需要先装什么",
        "",
        "只需要 Python 3.12 或更高版本（64 位）。",
        "",
        "服务端本体**没有第三方运行时依赖**，全部用标准库：",
        "    sqlite3 / struct / socket / threading / json / http.server / msgpack(自实现)",
        "",
        "要求里列出的 ruff / mypy / lupa 只是开发工具（lint、类型检查、Lua 回放测试），",
        "不装也能正常启动服务端。",
        "",
        "## 二、启动",
        "",
        "Windows PowerShell：",
        "    .\\start_all.ps1",
        "",
        "或手动起两个进程：",
        "    python -m web.app --port 8399",
        "    python -m game_server.server --ms-ok --unknown-ok",
        "",
        "端口：",
        "    33100  MS 微服务通道",
        "    33101  实体通道（游戏客户端连这个）",
        "    33102  管理接口（只监听 127.0.0.1）",
        "    8399   Web 控制台",
        "",
        "停止：",
        "    .\\stop_all.ps1",
        "",
        "## 三、第一次启动会自己建库",
        "",
        "shared/data.db 不在包里（里面是账号、密码哈希、会话 token，不该外传）。",
        "首启时 shared/db.py 的 init() 会自动建一个空库。",
        "",
        "所以**没有预置账号**，你需要先注册一个，再把自己提到 GM：",
        "    python -c \"import sys; sys.path.insert(0,'.'); from shared import db; db.set_perm_level('你的账号', 2)\"",
        "",
        "perm_level 语义：0=玩家  1=GM  2=管理员",
        "",
        "## 四、包里有什么 / 没什么",
        "",
        "启动器：",
        f"    {mark('launcher/dist/OO_Launcher.exe')}  launcher/dist/OO_Launcher.exe   启动器本体",
        f"    {mark('launcher/dist/OO_Internal.dll')}  launcher/dist/OO_Internal.dll   注入 DLL",
        f"    {mark('launcher/dist/OpenOrange.ini')}  launcher/dist/OpenOrange.ini    配置模板",
        f"    {mark('launcher/Cargo.toml')}  launcher/crates/                Rust 源码（可重新编译）",
        "",
        "没打的（拿不到是正常的）：",
        "    launcher/target/      Rust 编译缓存 981 MB",
        "    var/                  玩家存档 / 运行日志 / 抓包",
        "    shared/data.db*       账号库",
        "    Python/               本机专属解释器安装（含绝对路径，换机器就是坏的）",
        "    *.log  *.bak_*       运行日志与旧备份",
        "",
        "## 五、启动器要指向哪里",
        "",
        "launcher/dist/OpenOrange.ini 的 game_exe 默认是相对路径：",
        "    [Game]",
        "    game_exe = ..\\..\\..\\GuiLongchao\\game\\GuiLongchao.exe",
        "",
        "在你机器上按实际位置改成绝对路径，或者把本目录放进与 GuiLongchao 同级的结构里。",
        "",
        "## 六、常用入口",
        "",
        "    http://127.0.0.1:8399/              门户 / 登录",
        "    http://127.0.0.1:8399/admin/mail    发送邮件（GM 以上）",
        "    http://127.0.0.1:8399/admin/announce 公告 / 喊话",
        "    http://127.0.0.1:8399/admin/command 命令控制台",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 OpenOrange 服务端（不含运行垃圾与敏感数据）")
    ap.add_argument("--out", default="", help="输出 zip 路径；默认在仓库同级目录")
    ap.add_argument("--root", default=ROOT, help="要打包的仓库根（调试用）")
    ap.add_argument("--list", action="store_true", help="只列出会打进去的文件，不压缩")
    args = ap.parse_args(argv)

    out = args.out or os.path.join(
        os.path.dirname(ROOT),
        f"OpenOrange-server-{time.strftime('%Y%m%d-%H%M')}.zip",
    )

    if args.list:
        picked, skipped, total = collect(args.root)
        print(f"收录 {len(picked)} 个，{total / 1048576:.1f} MB；跳过 {skipped} 个")
        for rel, size in picked:
            print(f"  {size:>10}  {rel}")
        return 0

    print("===== 打包 OpenOrange 服务端 =====")
    print()
    build(args.root, out)
    print()
    print("已排除：")
    for d in EXCLUDE_DIRS:
        print(f"    {d}")
    for f in EXCLUDE_FILES:
        print(f"    {f}")
    print("    " + " / ".join(EXCLUDE_PATTERNS))
    print()
    print("拿到包的人第一次启动会自动建空库（db.init），需要自己注册账号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
