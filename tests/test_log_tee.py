#!/usr/bin/env python
"""统一日志组件（shared/log_tee.py）回归测试。

用法: python tests/test_log_tee.py
"""
import io
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = tempfile.mkdtemp(prefix="oo_test_logtee_")
os.environ["OO_LOG_DIR"] = _LOG_DIR
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_logtee_s_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_logtee_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shared import log_tee  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def read_last(channel):
    p = os.path.join(_LOG_DIR, "last.%s.log" % channel)
    if not os.path.exists(p):
        return ""
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def run():
    print("\n[1] install：文件创建 + stdout 拦截")
    orig_stdout = sys.stdout
    try:
        logger = log_tee.install("test", _LOG_DIR)
        check("返回 logger", logger is not None)
        run_path, last_path = log_tee.paths("test")
        check("run 文件带时间戳名",
              run_path and os.path.basename(run_path).endswith(".test.log")
              and os.path.basename(run_path) != "last.test.log", run_path)
        check("last 文件存在", os.path.exists(last_path), last_path)

        print("\n[2] print 落盘（含时间戳/级别）")
        print("hello-tee-info")
        sys.stderr.write("hello-tee-warn\n")
        sys.stdout.flush()
        content = read_last("test")
        check("stdout 内容在文件", "hello-tee-info" in content, content[-200:])
        check("stderr 内容在文件", "hello-tee-warn" in content, content[-200:])
        check("带时间戳前缀", "[INFO]" in content and "[WARNING]" in content,
              content[-200:])

        print("\n[3] 显式 logger 带级别过滤")
        lg = log_tee.get_logger("tee_mod")
        lg.debug("debug 应被文件过滤")      # 文件 level=INFO
        lg.info("info-tee-explicit")
        sys.stdout.flush()
        content = read_last("test")
        check("info 落盘", "info-tee-explicit" in content)
        check("debug 被过滤", "debug 应被文件过滤" not in content)

        print("\n[4] 多线程不撕行")
        def spam(tag):
            for i in range(50):
                print("line-%s-%04d %s" % (tag, i, "x" * 60))
        ts = [threading.Thread(target=spam, args=(t,)) for t in ("a", "b", "c")]
        for t in ts: t.start()
        for t in ts: t.join()
        sys.stdout.flush()
        content = read_last("test")
        torn = 0
        for t in ("a", "b", "c"):
            for i in range(50):
                if ("line-%s-%04d" % (t, i)) not in content:
                    torn += 1
        check("150 行全部完整（无撕裂）", torn == 0, torn)

        print("\n[5] traceback（stderr 多行）完整落盘")
        try:
            raise ValueError("boom-traceback")
        except ValueError:
            import traceback as _tb
            _tb.print_exc()
        sys.stdout.flush()
        content = read_last("test")
        check("异常类型在文件", "ValueError: boom-traceback" in content)

        print("\n[6] 轮转：30 份保留")
        for i in range(35):
            with open(os.path.join(
                    _LOG_DIR, "2026-01-01-00-00-%02d.test.log" % i), "w") as f:
                f.write("old")
        log_tee._rotate_keep(_LOG_DIR, "test")
        left = [f for f in os.listdir(_LOG_DIR)
                if f.endswith(".test.log") and f.startswith("2026-01-01")]
        check("旧时间戳文件被清到 30", len(left) <= 30, len(left))

        print("\n[7] 未 install 的进程（tests/tools 场景）get_logger 也能用")
        lg2 = log_tee.get_logger("no_install_mod")
        check("返回 logger 不抛异常", lg2 is not None)
    finally:
        # 恢复 stdout，别污染后续测试
        log_tee.reset_for_tests()
        sys.stdout = orig_stdout

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
