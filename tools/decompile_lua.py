#!/usr/bin/env python3
"""把解包出来的 .luac 批量反编译成 .lua。

用法:
    python tools/decompile_lua.py --luac <luac 目录> --out <输出目录> [--jobs 8]

反编译器用 unluac_glc.jar（针对本游戏的字节码改过的 unluac）。
注意必须用 JDK 运行，java 若不在 PATH 请用 --java 指定。
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def decompile_one(java, jar, src, dst):
    try:
        r = subprocess.run([java, "-jar", jar, src],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=120)
        if r.returncode != 0 or not r.stdout:
            return False, f'rc={r.returncode}'
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(r.stdout)
        return True, ""
    except Exception as e:
        return False, str(e)[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--luac", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--java", default="java")
    ap.add_argument("--jar", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()

    luac_dir = os.path.abspath(args.luac)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    tasks = []
    for root, _, files in os.walk(luac_dir):
        for f in files:
            if not f.endswith(".luac"):
                continue
            src = os.path.join(root, f)
            rel = os.path.relpath(src, luac_dir)
            dst = os.path.join(out_dir, rel[:-5] + ".lua")
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                continue
            tasks.append((src, dst))

    total = len(tasks)
    print("待反编译:", total, flush=True)
    if total == 0:
        return 0

    ok = fail = 0
    failed = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(decompile_one, args.java, args.jar, s, d): (s, d)
                for s, d in tasks}
        done = 0
        for fu in as_completed(futs):
            s, d = futs[fu]
            good, err = fu.result()
            done += 1
            if good:
                ok += 1
            else:
                fail += 1
                failed.append((os.path.relpath(s, luac_dir), err))
            if done % 1000 == 0:
                el = time.time() - t0
                print(f'  {done:d}/{total:d}  成功{ok:d} 失败{fail:d}  {el:.1f}s', flush=True)

    print()
    print(f'完成: 成功 {ok:d}, 失败 {fail:d}, 用时 {time.time() - t0:.1f} 秒')
    if failed:
        print("失败样例（前 20）:")
        for n, e in failed[:20]:
            print(f'  {n:<60} {e}')
        with open(os.path.join(out_dir, "_decompile_failed.txt"), "w", encoding="utf-8") as f:
            for n, e in failed:
                f.write(f"{n}\t{e}\n")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
