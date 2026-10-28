#!/usr/bin/env python3
"""跑 tests/ 下全部套件，汇总「套件数 / 项数 / 失败套件」。

用法：python -u tools/run_all_tests.py [-v]
  -v  额外打印每个套件的通过项数
"""
import contextlib
import os
import re
import subprocess
import sys
import tempfile

# ★ 测试不要往真实 work/logs 里写未实现请求日志（雷2）。
#   所有子套件共用一个临时文件即可（它们只验证自己写了几行）。
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_ulog_"), "unknown_requests.tsv"))

#测试要「造状态再验证」，造状态本来就该能直写属性（不是在走业务路径）。
#守卫对生产代码仍然生效 ——这个开关只在这个测试运行器里设。
os.environ["OO_ALLOW_PROPS_WRITE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = re.compile(r"通过\s*(\d+)\s*项，失败\s*(\d+)\s*项")


def main():
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
            print(f'{name:<30} ok={ok:<4d} fail={fail:<3d} rc={p.returncode:d}')
        if broken:
            lines = [l for l in out.split("\n")
                     if "FAIL" in l or "Error" in l or "Traceback" in l]
            for l in lines[:10]:
                print("    " + l)
    print("-" * 56)
    print(f'suites={len(files)}  items={total}  bad={bad}')
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
