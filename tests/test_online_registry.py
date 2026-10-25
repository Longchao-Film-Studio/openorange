#!/usr/bin/env python
"""在线连接表（game_server/transport/online.py）回归测试。

为什么专门测这张表（2026-10-26）：
  它是「服务端主动推送」的唯一入口。发错的后果不是报错，而是**静默丢消息**
  （表里没这条连接 -> 公告发不出去，没人发现）。另外它管着一把 per-conn 发送锁，
  锁失效会让两线程的字节交错、客户端解析崩——这类 bug 只能靠并发测试兜住。

用法: python tests/test_online_registry.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_online_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_online_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server.config import FALLBACK_UID  # noqa: E402
from game_server.transport import online  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


class FakeConn:
    """记录每次 sendall 的载荷；可配置成抛错（模拟对端已断）。"""

    def __init__(self, broken=False):
        self.payloads = []
        self.broken = broken

    def sendall(self, data):
        if self.broken:
            raise OSError("peer gone")
        self.payloads.append(bytes(data))


def frame(tag):
    """造一条可辨识的假帧。"""
    return b"[" + tag.encode() + b"]"


def run():
    online.reset_for_tests()

    print("\n[1] 登记 / 摘除 / 计数")
    c1, c2 = FakeConn(), FakeConn()
    online.register(1, c1)
    online.register(2, c2)
    check("count=2", online.count() == 2, online.count())
    check("未登录不计入已登录数", online.count(real_only=True) == 0, online.count(real_only=True))
    online.unregister(1)
    check("摘除后 count=1", online.count() == 1, online.count())
    check("重复摘除幂等", online.unregister(1) is False)
    online.reset_for_tests()

    print("\n[2] set_uid：FALLBACK -> 真实账号，以及换账号")
    online.register(10, FakeConn())
    online.set_uid(10, FALLBACK_UID)
    check("FALLBACK 不算已登录", online.uids() == [], online.uids())
    online.set_uid(10, "alice")
    check("登录后进 uids", online.uids() == ["alice"], online.uids())
    online.set_uid(10, "alice")
    check("重复 set 幂等", online.uids() == ["alice"], online.uids())
    online.set_uid(10, "bob")
    check("换账号后索引迁移", online.uids() == ["bob"], online.uids())
    online.reset_for_tests()

    print("\n[3] send：整组一次发 / 失败即摘除")
    c = FakeConn()
    online.register(20, c)
    ok = online.send(20, [frame("a"), frame("b")])
    check("send 返回 True", ok is True)
    check("整组合成一次 sendall", len(c.payloads) == 1, len(c.payloads))
    check("载荷拼接正确", c.payloads[0] == frame("a") + frame("b"), c.payloads)
    check("未登记的 cid 返回 False", online.send(999, [frame("x")]) is False)

    broken = FakeConn(broken=True)
    online.register(21, broken)
    online.set_uid(21, "carol")
    bad = online.send(21, [frame("y")])
    check("对端已断返回 False", bad is False)
    check("断开后自动摘除", online.count() == 1, online.count())

    print("\n[4] broadcast：只发给已登录 / 支持排除 / 统计正确")
    online.reset_for_tests()
    a, b, guest = FakeConn(), FakeConn(), FakeConn()
    online.register(30, a); online.set_uid(30, "u30")
    online.register(31, b); online.set_uid(31, "u31")
    online.register(32, guest)   # 未登录（FALLBACK）
    r = online.broadcast([frame("all")])
    check("targets=2（跳过未登录）", r["targets"] == 2, r)
    check("两人都收到", a.payloads and b.payloads)
    check("未登录没收到", guest.payloads == [], guest.payloads)
    r2 = online.broadcast([frame("ex")], exclude_cid=30)
    check("排除 cid 生效", r2["targets"] == 1, r2)
    check("被排除者没收到第二条", len(a.payloads) == 1, len(a.payloads))

    print("\n[5] send_to(uid)：同账号多连接（顶号/多开）")
    online.reset_for_tests()
    d1, d2 = FakeConn(), FakeConn()
    online.register(40, d1); online.set_uid(40, "same")
    online.register(41, d2); online.set_uid(41, "same")
    okn, failn = online.send_to("same", [frame("z")])
    check("两条连接都发到", (okn, failn) == (2, 0), (okn, failn))
    check("未知账号返回 (0,0)", online.send_to("nobody", [frame("z")]) == (0, 0))

    print("\n[6] 并发：登记/摘除/广播 同时跑不炸、表状态自洽")
    online.reset_for_tests()
    errors = []
    conns = {}

    def worker(i):
        try:
            conn = FakeConn()
            conns[i] = conn
            online.register(100 + i, conn)
            online.set_uid(100 + i, f"user{i}")
            online.send(100 + i, [frame(f"w{i}")])
            online.unregister(100 + i)
        except Exception as e:                    # noqa: BLE001
            errors.append(f"{i}: {e}")

    def broadcaster():
        try:
            for _ in range(50):
                online.broadcast([frame("bc")])
        except Exception as e:                    # noqa: BLE001
            errors.append(f"bc: {e}")

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    bc = threading.Thread(target=broadcaster)
    for t in ths: t.start()
    bc.start()
    for t in ths: t.join()
    bc.join()
    check("并发无异常", not errors, errors[:3])
    check("全部摘除后表为空", online.count() == 0, online.count())

    print("\n[7] 并发发送：同一连接上两线程写，帧不撕裂")
    online.reset_for_tests()
    slow = FakeConn()
    online.register(200, slow)
    online.set_uid(200, "slowpoke")

    def spam(tag):
        for i in range(60):
            online.send(200, [frame(f"{tag}-{i}")])

    t1 = threading.Thread(target=spam, args=("A",))
    t2 = threading.Thread(target=spam, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    bad = [p for p in slow.payloads
           if not p.startswith((b"[A-", b"[B-")) or p.count(b"[") != 1]
    check("120 次发送全部是完整单帧", len(slow.payloads) == 120 and not bad,
          (len(slow.payloads), bad[:2]))

    print("\n[8] broadcast_s2c：真的是合法 entityMessage")
    online.reset_for_tests()
    e = FakeConn()
    online.register(300, e); online.set_uid(300, "s2c")
    online.broadcast_s2c("sendWorldBanner", 882562061, ["测试公告"])
    check("发出一条帧", len(e.payloads) == 1, len(e.payloads))
    pkt = e.payloads[0] if e.payloads else b""
    check("含方法名", b"sendWorldBanner" in pkt, pkt[:60])
    check("帧头长度自洽", len(pkt) >= 6 and int.from_bytes(pkt[:4], "little") == len(pkt) - 4,
          (len(pkt), int.from_bytes(pkt[:4], "little") if len(pkt) >= 4 else None))

    print("\n[9] snapshot：排障视图")
    online.reset_for_tests()
    online.register(400, FakeConn())
    online.set_uid(400, "snap")
    online.send(400, [frame("s")])
    time.sleep(0.01)
    snap = online.snapshot()
    check("快照有 1 条", len(snap) == 1, snap)
    check("含 uid 与发送计数", snap and snap[0]["uid"] == "snap" and snap[0]["frames_sent"] == 1, snap)

    online.reset_for_tests()
    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
