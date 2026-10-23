#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同账号并发锁（雷5）回归测试。

背景：PlayerState 按 uid 共享，业务 handler 大量直接写 st.props[...]，
player_state._LOCK 只保护 get/set/save，挡不住「读-改-写」交错。
同一账号双开/重连竞态时会丢物品/丢任务状态。

现在：player_state.lock_for(uid) 提供 per-uid 可重入锁，
入口（dispatch.handle / 心跳 / 登录初始化 / 管理接口）统一加锁。

用法: python tests/test_player_lock.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_lock_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_lock_db_"), "data.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_lock_log_"), "unknown_requests.tsv")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import player_state as pstate  # noqa: E402
from game_server.transport import accounts, gate  # noqa: E402
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


class _Spy:
    """记录 lock_for 被哪些 uid 调用过。"""

    def __init__(self, real):
        self.real = real
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, uid=None):
        with self._lock:
            self.calls.append(uid)
        return self.real(uid)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def sendall(self, pkt):
        self.sent.append(pkt)


def run():
    pstate.clear_uid_locks()

    print("\n[1] 锁的身份：同 uid 同一把，不同 uid 不同")
    a1 = pstate.lock_for("lock_a")
    a2 = pstate.lock_for("lock_a")
    b1 = pstate.lock_for("lock_b")
    check("同 uid 返回同一个锁对象", a1 is a2)
    check("不同 uid 返回不同锁对象", a1 is not b1)
    check("是 RLock（可重入）", hasattr(a1, "acquire") and hasattr(a1, "release"))

    print("\n[2] 可重入：同一线程嵌套获取不死锁")
    ok = True
    try:
        with pstate.lock_for("lock_reentrant"):
            with pstate.lock_for("lock_reentrant"):
                pass
    except Exception:
        ok = False
    check("嵌套获取成功", ok)

    print("\n[3] 并发正确性：4 线程 × 500 次读改写，一次都不能丢")
    st = pstate.get_state(uid="lock_counter")
    st.props["n"] = 0
    rounds, threads = 500, 4

    def worker():
        for _ in range(rounds):
            with pstate.lock_for("lock_counter"):
                cur = st.props.get("n", 0)
                # 制造调度窗口：读与写之间让出 GIL
                if cur % 7 == 0:
                    time.sleep(0)
                st.props["n"] = cur + 1

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    check("最终计数 = %d" % (rounds * threads),
          st.props.get("n") == rounds * threads, st.props.get("n"))

    print("\n[4] dispatch.handle 确实走 per-uid 锁")
    spy = _Spy(pstate.lock_for)
    old = pstate.lock_for
    pstate.lock_for = spy
    try:
        guarded_handle(1158350933, b"\x90", 1, uid="lock_dispatch")
    finally:
        pstate.lock_for = old
    check("handle 调用了 lock_for(uid)",
          "lock_dispatch" in spy.calls, spy.calls)

    print("\n[5] 心跳路径也确实走同一把锁")
    accounts.bind(901, "lock_heartbeat")
    spy2 = _Spy(pstate.lock_for)
    old = pstate.lock_for
    pstate.lock_for = spy2
    try:
        gate._on_heartbeat(_FakeConn(), 901)
    finally:
        pstate.lock_for = old
    check("心跳调用了 lock_for(uid)", "lock_heartbeat" in spy2.calls, spy2.calls)

    print("\n[6] 登录初始化（_live_init_dict）走同一把锁")
    from game_server.ms import initdict
    spy3 = _Spy(pstate.lock_for)
    old = pstate.lock_for
    pstate.lock_for = spy3
    try:
        initdict._live_init_dict("lock_login")
    finally:
        pstate.lock_for = old
    check("initDict 调用了 lock_for(uid)", "lock_login" in spy3.calls, spy3.calls)

    print("\n[7] 不同 uid 的锁互不阻塞")
    held = pstate.lock_for("lock_x")
    got = {"v": False}

    def other():
        with pstate.lock_for("lock_y"):
            got["v"] = True

    with held:
        t = threading.Thread(target=other)
        t.start()
        t.join(timeout=5)
    check("持 lock_x 时 lock_y 仍可获得", got["v"])

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
