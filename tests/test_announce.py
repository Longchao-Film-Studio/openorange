#!/usr/bin/env python
"""全服公告模块（game_server/announce.py）单元测试。

为什么值得单独测：
  公告是「发出去了但没人看见」风险最高的一类功能——发错索引/发错实体都不会报错，
  只是静默无效。所以这里锁死三件事：
    1. 三条通道的索引与 rpc_list.tsv 一致（不许手抄漂移）
    2. 长度上限（跑马灯滚动时长按字数线性增长，太长会按着玩家看广告）
    3. 目标语义（全服 / 指定账号 / 没人在线时诚实失败）

用法: python tests/test_announce.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_announce_")
os.environ["OO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="oo_test_announce_db_"), "data.db")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import announce  # noqa: E402
from game_server import ms as msmod
from game_server.transport import online  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


class FakeConn:
    def __init__(self, broken=False):
        self.payloads = []
        self.broken = broken

    def sendall(self, data):
        if self.broken:
            raise OSError("peer gone")
        self.payloads.append(bytes(data))


def rpc_list_index(name):
    """从 work/rpc_list.tsv 现查索引（防手抄漂移）。"""
    path = os.path.join(ROOT, "data", "generated", "rpc_list.tsv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 3 and cols[1] == name:
                return int(cols[2])
    return None


def run():
    online.reset_for_tests()
    announce.reset_for_tests()

    print("\n[1] 三条通道的索引必须与 rpc_list.tsv 一致")
    for kind, (method, index, _limit) in announce.KINDS.items():
        real = rpc_list_index(method)
        check(f"{kind} -> {method} 索引一致（{index}）", real == index,
              f"rpc_list={real} KINDS={index}")

    print("\n[2] build_frame：是合法 entityMessage，方法名/参数都对")
    pkt = announce.build_frame("banner", "测试公告")
    _mtype, method, _rid, params = msmod.parse_entity_response(pkt)
    check("方法名 = sendWorldBanner", method == "sendWorldBanner", method)
    check("参数是 [文案]", params == ["测试公告"], params)
    check("帧头长度自洽", int.from_bytes(pkt[:4], "little") == len(pkt) - 4,
          (len(pkt), int.from_bytes(pkt[:4], "little")))
    for kind, (method, _i, _l) in announce.KINDS.items():
        p2 = announce.build_frame(kind, "x")
        _t, m2, _r, _p = msmod.parse_entity_response(p2)
        check(f"{kind} 帧方法名 = {method}", m2 == method, m2)

    print("\n[3] 参数校验：空文案 / 未知类型 / 超长")
    r = announce.announce("", "banner")
    check("空文案拒绝", r["ok"] is False and "不能为空" in r["error"], r)
    r = announce.announce("  ", "toast")
    check("纯空白也拒绝", r["ok"] is False, r)
    r = announce.announce("hi", "nope")
    check("未知类型拒绝", r["ok"] is False and "未知类型" in r["error"], r)
    r = announce.announce("长" * 81, "banner")
    check("跑马灯 80 字上限", r["ok"] is False and "最多 80" in r["error"], r)
    r = announce.announce("长" * 200, "toast")
    check("Toast 200 字上限（刚好允许，但没人在线会另报）",
          "最多" not in (r.get("error") or ""), r)
    r = announce.announce("长" * 201, "toast")
    check("Toast 201 字拒绝", r["ok"] is False and "最多 200" in r["error"], r)

    print("\n[4] 没有在线玩家时：诚实失败，不谎报成功")
    r = announce.announce("有人吗", "banner")
    check("ok=False", r["ok"] is False, r)
    check("错误文案说清原因", "没有在线玩家" in (r.get("error") or ""), r)

    print("\n[5] 全服广播：只发已登录连接")
    a, b, guest = FakeConn(), FakeConn(), FakeConn()
    online.register(1, a); online.set_uid(1, "u1")
    online.register(2, b); online.set_uid(2, "u2")
    online.register(3, guest)                      # 未登录
    r = announce.announce("全服公告", "banner")
    check("ok=True", r["ok"] is True, r)
    check("targets=2（跳过未登录）", r["targets"] == 2, r)
    check("两条已登录连接都收到", a.payloads and b.payloads)
    check("未登录没收到", guest.payloads == [], guest.payloads)
    _t, m, _ri, p = msmod.parse_entity_response(a.payloads[0])
    check("收到的是 sendWorldBanner + 文案", m == "sendWorldBanner" and p == ["全服公告"],
          (m, p))

    print("\n[6] 定向发送：只给指定账号的所有连接")
    online.reset_for_tests()
    d1, d2, other = FakeConn(), FakeConn(), FakeConn()
    online.register(10, d1); online.set_uid(10, "same")
    online.register(11, d2); online.set_uid(11, "same")   # 同账号第二条连接（顶号/多开）
    online.register(12, other); online.set_uid(12, "other")
    r = announce.announce("只给你", "toast", uid="same")
    check("ok=True 且 targets=2", r["ok"] is True and r["targets"] == 2, r)
    check("目标账号两条连接都收到", d1.payloads and d2.payloads)
    check("其他账号没收到", other.payloads == [], other.payloads)
    r = announce.announce("查无此人", "toast", uid="ghost")
    check("不在线账号诚实失败", r["ok"] is False and "不在线" in r["error"], r)

    print("\n[7] 历史：记录成功与失败，新的在前，reset 可清")
    announce.reset_for_tests()
    online.reset_for_tests()
    online.register(20, FakeConn()); online.set_uid(20, "h1")
    announce.announce("第一条", "banner")
    announce.announce("第二条", "toast")
    announce.announce("", "banner")           # 失败也记
    hist = announce.history(10)
    check("历史条数 3", len(hist) == 3, len(hist))
    check("新的在前", hist[0]["text"] == "" and hist[1]["text"] == "第二条", hist[0])
    check("失败项 ok=False", hist[0]["ok"] is False, hist[0])
    check("历史带上目标计数", hist[1]["ok_count"] == 1, hist[1])
    announce.reset_for_tests()
    check("reset 后为空", announce.history(10) == [])

    print("\n[8] kinds() 给管理页的类型清单")
    ks = announce.kinds()
    check("三类都列出", sorted(k["kind"] for k in ks) == ["banner", "gm", "toast"], ks)
    check("带索引与上限", all("index" in k and "limit" in k for k in ks), ks)

    online.reset_for_tests()
    announce.reset_for_tests()
    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
