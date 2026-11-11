#!/usr/bin/env python3
"""跑 tests/ 下全部套件，汇总「套件数 / 项数 / 失败套件」。

用法：
    python -u tools/run_all_tests.py              # 默认：并行（每套件独立子进程）
    python -u tools/run_all_tests.py -v           # 额外打印每个套件的通过项数
    python -u tools/run_all_tests.py --jobs 4     # 限制并行度
    python -u tools/run_all_tests.py --serial     # 串行（对照 / 调试用）

## 为什么要并行（2026-11-11）

原来每个套件顺序 spawn 一个 python 子进程，串行跑满 100 秒左右。
107 个套件里绝大多数只跑几百毫秒，**瓶颈是进程启动 + import**
（实测 import dispatch 2.0s），所以按核数并行就能直接摊薄。

## 为什么**不做**「一个进程连跑多个套件」

试过，被实测否掉了：进程内共享 game_server 的模块级全局，
导致套件之间互相污染 —— 实测 5 个套件因此变红：

    test_unlock_sync       开局就看到 {2:True,4:True,55:True}（上个套件留下的）
    test_admin_console     ValueError: unknown url type: '/'（全局 base_url 被改）
    test_admin_announce    同上
    test_account_portal    同上
    test_unknown_handling  日志路径指向别的套件的临时目录

要做对就得枚举并重置所有模块级可变全局，那是无底洞。
每个套件一个**全新解释器**是唯一可靠的隔离，代价只是启动开销 ——
而启动开销正好被并行摊掉。所以：**并行 + 独立进程**，不做进程内复用。

★ 正确性判据：默认模式与 --serial 必须给出相同的 suites / items / bad。
"""
import argparse
import concurrent.futures as _fut
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import time

# ★ 测试不要往真实 work/logs 里写未实现请求日志（雷2）。
os.environ.setdefault(
    "OO_UNKNOWN_LOG",
    os.path.join(tempfile.mkdtemp(prefix="oo_test_ulog_"), "unknown_requests.tsv"))

# 测试要「造状态再验证」，造状态本来就该能直写属性（不是在走业务路径）。
# 守卫对生产代码仍然生效 —— 这个开关只在这个测试运行器里设。
os.environ["OO_ALLOW_PROPS_WRITE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY = re.compile(r"通过\s*(\d+)\s*项，失败\s*(\d+)\s*项")

#: 进入任何套件之前的干净环境。每个套件导入前都会恢复成这个。
_BASE_ENV = dict(os.environ)

#: 单套件超时（秒）。超了就杀掉整个 worker，剩余套件记为失败 ——
#: 挂住本身就是要人来处理的故障，不该静默吞掉。
DEFAULT_SUITE_TIMEOUT = 180

sys.path.insert(0, ROOT)


def _suite_names():
    return sorted(f[:-3] for f in os.listdir(os.path.join(ROOT, "tests"))
                  if f.startswith("test_") and f.endswith(".py"))


def _parse(out):
    m = SUMMARY.search(out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, -1)


# ---------------------------------------------------------------- 旧路径
def _run_subprocess(name):
    """旧行为：一个套件一个子进程。作为对照基线与逃生口保留。"""
    p = subprocess.run([sys.executable, "-u", "-m", "tests." + name],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (p.stdout or "") + (p.stderr or "")
    ok, fail = _parse(out)
    return {"name": name, "ok": ok, "fail": fail, "rc": p.returncode, "out": out}


# ---------------------------------------------------------------- 新路径
# ---------------------------------------------------------------- 调度
def _order(names):
    """按文件大小从大到小排 —— 大套件先发，各 worker 收尾更齐。"""
    td = os.path.join(ROOT, "tests")

    def cost(n):
        with contextlib.suppress(OSError):
            return os.path.getsize(os.path.join(td, n + ".py"))
        return 0

    return sorted(names, key=cost, reverse=True)


def _report(results, verbose, expected=None):
    """汇总。

    ★ expected：本次应当跑到的套件总数。并行调度最大的风险是**静默丢套件**，
      而丢了套件之后 bad 仍然是空数组 —— 门禁会假绿。所以这里显式对账：
      少了就把缺的套件名打成失败，绝不让「少跑了」伪装成「全过了」。
    """
    total = 0
    bad = []
    if expected is not None:
        got = {r["name"] for r in results}
        missing = sorted(set(expected) - got)
        for n in missing:
            results.append({"name": n, "ok": 0, "fail": -1, "rc": 1,
                            "out": "没有返回结果（调度丢了套件）"})
    for r in sorted(results, key=lambda x: x["name"]):
        total += r["ok"]
        broken = r["fail"] != 0 or r["rc"] != 0
        if broken:
            bad.append(r["name"])
        if verbose or broken:
            print(f'{r["name"]:<30} ok={r["ok"]:<4d} fail={r["fail"]:<3d} rc={r["rc"]:d}')
        if broken:
            lines = [l for l in r["out"].split("\n")
                     if "FAIL" in l or "Error" in l or "Traceback" in l]
            if not lines:
                # 没有 FAIL/Error 行时也要说清楚为什么算失败，
                # 否则「调度丢了套件」这类故障只看到一个 rc=1，没有原因。
                lines = [l for l in r["out"].split("\n") if l.strip()][:3]
            for l in lines[:10]:
                print("    " + l)
    print("-" * 56)
    print(f'suites={len(results)}  items={total}  bad={bad}')
    return 1 if bad else 0


def main():
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--serial", action="store_true", help="串行（对照/调试用）")
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数，默认 min(8, CPU)")
    args = ap.parse_args()
    os.chdir(ROOT)

    names = _order(_suite_names())
    t0 = time.time()

    if args.serial:
        results = [_run_subprocess(n) for n in names]
        rc = _report(results, args.v, expected=names)
        print(f"（serial，{time.time() - t0:.1f}s）")
        return rc

    jobs = args.jobs or min(8, os.cpu_count() or 4)
    jobs = max(1, min(jobs, len(names)))
    results = []
    with _fut.ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_run_subprocess, n): n for n in names}
        for fut in _fut.as_completed(futs):
            name = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001 —— 子进程自身炸了也要记账
                results.append({"name": name, "ok": 0, "fail": -1, "rc": 1,
                                "out": f"子进程异常：{e}"})

    rc = _report(results, args.v, expected=names)
    print(f"（{jobs} 并行 / {len(names)} 套件，{time.time() - t0:.1f}s）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
