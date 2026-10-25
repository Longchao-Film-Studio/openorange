#!/usr/bin/env python3
"""扫描归龙潮的 AssetBundle，列出所有资源容器路径，找关键字。

只读容器路径（AssetBundle 的 m_Container），不解析资源内容，速度最快。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import unpack_bundle as U

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import UnityPy


def bundle_containers(path):
    data = open(path, "rb").read()
    info = U.parse_bundle(data)
    stream = info["stream"]
    out = []
    for node in info["nodes"]:
        if node["path"].endswith(".resS") or node["path"].endswith(".resource"):
            continue
        blob = stream[node["offset"]:node["offset"] + node["size"]]
        if len(blob) < 32:
            continue
        try:
            env = UnityPy.load(blob)
            for o in env.objects:
                c = o.container
                if c:
                    out.append((c, o.type.name))
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--find", default="")
    ap.add_argument("--dump", default="", help="把所有容器路径写到这个 json")
    ap.add_argument("--min-size", type=int, default=0)
    ap.add_argument("--max-size", type=int, default=0)
    args = ap.parse_args()

    kws = [k.lower() for k in args.find.split(",") if k]
    files = []
    for dp, _, fs in os.walk(args.dir):
        for f in fs:
            if f.lower().endswith(".dab"):
                fp = os.path.join(dp, f)
                sz = os.path.getsize(fp)
                if args.min_size and sz < args.min_size:
                    continue
                if args.max_size and sz > args.max_size:
                    continue
                files.append((sz, fp))
    files.sort()

    print('待扫 {} 个包'.format(len(files)), flush=True)
    t0 = time.time()
    allc = {}
    hits = 0
    for i, (sz, fp) in enumerate(files):
        try:
            conts = bundle_containers(fp)
        except Exception:
            continue
        allc[os.path.basename(fp)] = conts
        if kws:
            for c, t in conts:
                if any(k in c.lower() for k in kws):
                    hits += 1
                    print("  *** %-28s %-18s %s" % (os.path.basename(fp), t, c), flush=True)
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            print("  ...%d/%d  用时 %.0fs  已收 %d 条路径  命中 %d"
                  % (i + 1, len(files), el, sum(len(v) for v in allc.values()), hits), flush=True)

    print("完成：%d 个包，用时 %.0fs，路径 %d 条，命中 %d"
          % (len(files), time.time() - t0, sum(len(v) for v in allc.values()), hits), flush=True)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(allc, f, ensure_ascii=False, indent=1)
        print("已写出", args.dump, flush=True)


if __name__ == "__main__":
    main()
