#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑 tests/ 下全部套件，汇总「套件数 / 项数 / 失败套件」。

用法：python -u tools/run_all_tests.py [-v]
  -v  额外打印每个套件的通过项数
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = re.compile(r"\u901a\u8fc7 (\d+) \u9879\uff0c\u5931\u8d25 (\d+) \u9879")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    verbose = "-v" in sys.argv[1:]
    os.chdir(ROOT)
    files = sorted(f[:-3] for f in os.listdir("tests")
                   if f.startswith("test_") and f.endswith(".py"))
    total = 0
    bad = []
    for name in files:
        p = subprocess.run([sys.executable, "-u", "-m", "tests." + name],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        m = SUMMARY.search(out)
        ok, fail = (int(m.group(1)), int(m.group(2))) if m else (0, -1)
        total += ok
        broken = fail != 0 or p.returncode != 0
        if broken:
            bad.append(name)
        if verbose or broken:
            print("%-30s ok=%-4d fail=%-3d rc=%d" % (name, ok, fail, p.returncode))
        if broken:
            lines = [l for l in out.split("\n")
                     if "FAIL" in l or "Error" in l or "Traceback" in l]
            for l in lines[:10]:
                print("    " + l)
    print("-" * 56)
    print("suites=%d  items=%d  bad=%s" % (len(files), total, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
