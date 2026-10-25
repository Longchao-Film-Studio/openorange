#!/usr/bin/env python3
"""统一日志（logging 模块 + 级别）。

文件布局（config.LOG_DIR，默认 work/logs/）：
  YYYY-MM-DD-HH-MM-SS.{channel}.log   本次运行（每次启动新建）
  last.{channel}.log                  最近一次运行（每次启动截断重写）
  （时间戳文件默认保留最近 30 份，防无限增长）

设计决策（2026-10-24）：
- 用标准 logging，不用 print 改造：**print 调用点全部保留**，
  通过 LoggingInterceptor 劫持 stdout/stderr，把现有 246 处 print()
  的输出按级别（stdout->INFO，stderr->WARNING）收进同一套文件。
  这是最小侵入路径 —— 任何 traceback（走 stderr）也自动落盘，崩溃现场不再丢。
- 新代码建议直接用 get_logger(__name__)，可带级别过滤；旧 print 无需迁移。
- 多线程安全：logging 自带每条记录的锁，thread-per-connection 不会撕行。
- tests/tools 不装拦截器（各自进程，不经 main()），行为不变。
"""
import logging
import os
import re
import sys
import threading
import time

# ---------------------------------------------------------------- 级别
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR

_channel_handlers = {}
_install_lock = threading.Lock()
_intercepted = False


class _Formatter(logging.Formatter):
    """[MM-DD HH:MM:SS] [LEVEL] [thread] 消息。"""

    def __init__(self):
        super().__init__(fmt="[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s",
                         datefmt="%m-%d %H:%M:%S")


class _PrintInterceptor:
    """把 stdout/stderr 的写入转成 logging 记录。

    - 保留原始流（控制台还能看到，行为与以前一致）
    - 整行缓冲：按 \n 切，半行（flush=True 的碎片）等下一片
    - 防递归：logging 内部如果再 print，直接写原始流
    """

    def __init__(self, original, logger, level):
        self._original = original
        self._logger = logger
        self._level = level
        self._logger.propagate = True   # 拦截器消息要到达 parent 的文件 handler
        self._buf = ""
        self._lock = threading.Lock()
        self._in_log = threading.local()

    def write(self, s):
        try:
            if self._in_log.value:
                return self._original.write(s)
        except AttributeError:
            pass
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit(line)
        return len(s)

    def _emit(self, line):
        self._in_log.value = True
        try:
            self._logger.log(self._level, line.rstrip("\r"))
        finally:
            self._in_log.value = False

    def flush(self):
        try:
            if not self._in_log.value and self._buf:
                with self._lock:
                    line, self._buf = self._buf, ""
                    self._emit(line)
        finally:
            try:
                self._original.flush()
            except Exception:
                pass

    # ---- 让 print/库把它当流用 ----
    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        try:
            return self._original.fileno()
        except Exception:
            raise OSError("intercepted stream has no fileno")

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")


def _rotate_keep(log_dir, channel, keep=30):
    """时间戳日志只留最近 keep 份。"""
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\." + re.escape(channel) + r"\.log$")
    files = []
    d = log_dir
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        if pat.match(f):
            p = os.path.join(d, f)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    files.sort(reverse=True)
    for _, p in files[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass


def install(channel, log_dir, level=logging.INFO):
    """给本进程装日志：文件双写 + 控制台 + stdout/stderr 拦截。

    channel: "game" / "web"（文件名后缀）
    log_dir: 日志目录（调用方从自己的 config 取，模块不反向依赖）
    level:  文件里记录的最低级别（控制台始终全量，保持旧行为）
    返回该 channel 的 logger。
    """
    global _intercepted
    with _install_lock:
        if channel in _channel_handlers:
            return get_logger(channel)

        os.makedirs(log_dir, exist_ok=True)
        _rotate_keep(log_dir, channel)

        ts = time.strftime("%Y-%m-%d-%H-%M-%S")
        run_path = os.path.join(log_dir, "{}.{}.log".format(ts, channel))
        last_path = os.path.join(log_dir, "last.{}.log".format(channel))

        # 文件/控制台 handler 挂在 "oo" 父 logger 上 ——
        # 这样 get_logger("任意模块名")（=oo.任意模块名）都 propagate 到同一批文件
        parent = logging.getLogger("oo")
        parent.setLevel(logging.DEBUG)
        parent.propagate = False
        fmt = _Formatter()

        for p in (run_path, last_path):
            fh = logging.FileHandler(p, mode="w", encoding="utf-8")
            fh.setLevel(level)
            fh.setFormatter(fmt)
            parent.addHandler(fh)

        ch = logging.StreamHandler(sys.stdout)   # 控制台：走原 stdout，避免自递归
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(fmt)
        parent.addHandler(ch)

        logger = logging.getLogger("oo." + channel)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False    # channel 自己不再向上（handler 在 parent 上，防双写）
        _channel_handlers[channel] = (run_path, last_path)

        if not _intercepted:
            sys.stdout = _PrintInterceptor(sys.stdout, logger, logging.INFO)
            sys.stderr = _PrintInterceptor(sys.stderr, logger, logging.WARNING)
            _intercepted = True

        logger.info("=== 日志启动 channel=%s run=%s ===", channel, run_path)
        return logger


def get_logger(name):
    """业务代码的显式日志入口：log = get_logger(__name__)。

    未 install 时也能用（挂到 root，只走控制台）——tests/tools 不受影响。
    """
    return logging.getLogger("oo." + name if not name.startswith("oo.") else name)


def paths(channel):
    """当前 channel 的 (run_path, last_path)。测试/排障用。"""
    return _channel_handlers.get(channel, (None, None))


def reset_for_tests():
    """测试用：拆掉拦截器与 handler，恢复原流。"""
    global _intercepted
    with _install_lock:
        for ch in _channel_handlers:
            logger = logging.getLogger("oo." + ch)
            logger.propagate = False
        parent = logging.getLogger("oo")
        for h in list(parent.handlers):
            try:
                h.close()
            except Exception:
                pass
            parent.removeHandler(h)
        _channel_handlers.clear()
        _intercepted = False
