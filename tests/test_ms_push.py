#!/usr/bin/env python3
"""邮件推送（M6）服务端测试：帧格式 / 连接表 / bind / 发信触发推送。

用法:
    python tests/test_ms_push.py

被测的不变量：

    INV-PUSH-1  MicroPush 是帧类型 4，字段顺序 service/method/params/msg_uuid/ep_ids/org_type
    INV-PUSH-2  ms_conns 是唯一写 MS socket 的入口；未 bind 的连接推不到
    INV-PUSH-3  ClientGate.bind 成功后才建立 uid<->连接 的映射
    INV-PUSH-4  send_mail 发完自动推送（下游不用各自记得推）
    INV-PUSH-5  不在线时推送静默跳过，不影响发信
    INV-PUSH-6  msAuth 派生自 uid 且非空（客户端不 bind 的根因就在这里）
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_push_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import config as ooconf  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import prop_origin  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.ms import frames as msframes  # noqa: E402
from game_server.transport import ms_conns  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402
from tools.xlua import read_descriptor  # noqa: E402

UID = "push_u"
TPL = 1

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


class FakeSock:
    def __init__(self):
        self.frames = []

    def sendall(self, data):
        self.frames.append(bytes(data))


#: MsPush 的**真实**字段号，从解密镜像的 FileDescriptorProto 里逐字节读出来。
#  ★ 不手写常量：2026-10-31 那次事故就是因为代码和测试共用同一份错误推断（写成 1..6，
#    真实 2..7），推送静默失效而测试全绿。测试必须能独立拿到真相。
#    镜像不存在时返回 None，下面的用例会跳过而不是假装通过。
MS_PUSH_FIELDS = read_descriptor.message_fields("ms_common.proto", "MsPush")


MS_PUSH_FIELDS_INV = ({nm: num for num, nm in MS_PUSH_FIELDS.items()}
                      if MS_PUSH_FIELDS else {})


def _f(name, default):
    """按字段名取真实字段号（拿不到描述符时退回 default，仅用于让用例不至于崩）。"""
    if MS_PUSH_FIELDS:
        for num, nm in MS_PUSH_FIELDS.items():
            if nm == name:
                return num
    return default


def parse_push(frame):
    """把一帧 MicroPush 解回字段字典（用描述符里的真实字段号）。"""
    mtype = int.from_bytes(frame[4:6], "little")
    body = msmod.pb_parse(frame[6:])
    def _s(field, default):
        # ★ 容错解码：字段号如果错了，这里拿到的是二进制，直接 .decode() 会抛
        #   UnicodeDecodeError —— 那样破坏性验证只会看到 traceback，看不出"字段号错了"。
        raw = body.get(_f(field, default), [b""])[0]
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    return {
        "type": mtype,
        "service": _s("service", 2),
        "method": _s("method", 3),
        "params": body.get(_f("params", 4), [b""])[0],
        "msg_uuid": _s("msg_uuid", 5),
        "ep_ids": [b.decode("utf-8", "replace") for b in body.get(_f("ep_ids", 6), [])
                   if isinstance(b, bytes)],
        "org_type": _s("org_type", 7),
    }


def fresh(uid=UID):
    path = os.path.join(os.environ["OO_STATE_DIR"], uid + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(uid, None)
    msmod.live_init_dict(uid=uid)
    return pstate.STATES[uid]


def main():
    print("tmp =", _TMP)
    svc._ensure_handlers_loaded()
    ms_conns.reset_for_tests()

    # ------------------------------------------------------------ INV-PUSH-1
    print("\n[1] INV-PUSH-1 帧结构：类型 4 + 六个字段按顺序")
    params = msmod.mp_arr([[{"MailId": 1}]])
    f = msframes.frame_ms_push("ENTITY", ["push_u"], "MailService", "newMailNotice", params)
    p = parse_push(f)
    check("帧类型是 4（MicroPush）", p["type"] == 4, p["type"])
    check("service = MailService", p["service"] == "MailService", p["service"])
    check("method = newMailNotice", p["method"] == "newMailNotice", p["method"])
    check("params 原样带过去", p["params"] == params)
    check("org_type = ENTITY", p["org_type"] == "ENTITY", p["org_type"])
    check("ep_ids = [uid]", p["ep_ids"] == ["push_u"], p["ep_ids"])
    check("msg_uuid 为空时不写字段 4", p["msg_uuid"] == "", p["msg_uuid"])
    f2 = msframes.frame_ms_push("ENTITY", ["u"], "S", "m", params, msg_uuid="abc")
    check("msg_uuid 非空时写字段 4", parse_push(f2)["msg_uuid"] == "abc")
    check("客户端解出的参数是数组、第一个元素是邮件列表",
          msmod.mp_decode(p["params"])[0] == [[{"MailId": 1}]], msmod.mp_decode(p["params"])[0])

    print("\n[1b] 对照描述符：发出的字段号必须与 ms_common.proto 完全一致")
    if MS_PUSH_FIELDS is None:
        print("  SKIP：读不到 xlua 镜像（tools/xlua/read_descriptor.py 的 IMAGE）")
    else:
        check("描述符里 MsPush 有 6 个字段", len(MS_PUSH_FIELDS) == 6, MS_PUSH_FIELDS)
        check("字段名集合与契约一致",
              set(MS_PUSH_FIELDS.values()) ==
              {"service", "method", "params", "msg_uuid", "ep_ids", "org_type"},
              MS_PUSH_FIELDS)
        body_keys = set(msmod.pb_parse(f[6:]))
        # 这一帧没传 msg_uuid（空串在 proto3 里不写），所以允许它缺席；其余必须全在
        optional_empty = {MS_PUSH_FIELDS_INV["msg_uuid"]}
        check("我们写的字段号 ⊂ 描述符里的字段号",
              body_keys <= set(MS_PUSH_FIELDS), (sorted(body_keys), sorted(MS_PUSH_FIELDS)))
        check("除 msg_uuid（空值不写）外，描述符里的字段一个不少",
              set(MS_PUSH_FIELDS) - optional_empty == body_keys,
              (sorted(body_keys), sorted(set(MS_PUSH_FIELDS) - optional_empty)))
        f_all = msframes.frame_ms_push("ENTITY", ["u"], "S", "m", params, msg_uuid="abc")
        check("传了 msg_uuid 时六个字段全写",
              set(msmod.pb_parse(f_all[6:])) == set(MS_PUSH_FIELDS),
              (sorted(msmod.pb_parse(f_all[6:])), sorted(MS_PUSH_FIELDS)))
        # 逐个字段名再核一遍：防止"号对了但填错了位置"
        got = parse_push(f)
        check("service 写在描述符指定的号上",
              got["service"] == "MailService" and
              msmod.pb_parse(f[6:]).get(MS_PUSH_FIELDS_INV["service"]) is not None,
              got["service"])
        check("org_type 写在描述符指定的号上",
              got["org_type"] == "ENTITY" and
              MS_PUSH_FIELDS_INV["org_type"] in body_keys, got["org_type"])
        # 帧类型 4 也对着描述符的 EnumG2C.Type 核
        g2c = read_descriptor.enum_values("ms_common.proto", "Type", in_message="EnumG2C")
        if g2c:
            check("帧类型 == EnumG2C.Type.MicroPush",
                  p["type"] == g2c.get("MicroPush"), (p["type"], g2c))
            check("MicroResponse 也一致（回包一直是对的）",
                  g2c.get("MicroResponse") == msframes.MS_RESP_TYPE,
                  (g2c.get("MicroResponse"), msframes.MS_RESP_TYPE))

    # ------------------------------------------------------------ INV-PUSH-2
    print("\n[2] INV-PUSH-2 连接表：未 bind 推不到，bind 后推得到")
    sock = FakeSock()
    ms_conns.register(1, sock)
    check("未 bind 时 push 返回 False", ms_conns.push(UID, b"x") is False)
    check("socket 没收到东西", sock.frames == [], len(sock.frames))

    # ------------------------------------------------------------ INV-PUSH-3
    print("\n[3] INV-PUSH-3 ClientGate.bind 建立映射")
    bind_fields = {4: [msmod.mp_arr([UID, "oo_" + UID, 20000])]}
    ret = msmod.mp_decode(svc.ms_reply_params("ClientGate", "bind", bind_fields, 1))[0]
    check("bind 回成功", ret["Ret"][0] is True, ret)
    check("uid 绑到了 cid=1", ms_conns.cid_of(UID) == 1, ms_conns.cid_of(UID))
    check("反查 uid 也对", ms_conns.uid_of(1) == UID, ms_conns.uid_of(1))
    check("bind 后 push 成功", ms_conns.push(UID, b"hello") is True)
    check("socket 收到了", sock.frames == [b"hello"], sock.frames)
    check("缺 id 的 bind 回失败",
          msmod.mp_decode(svc.ms_reply_params("ClientGate", "bind", {4: [msmod.mp_arr([])]}, 1))[0]["Ret"][0] is False)

    print("\n[4] 断开后不再推（unregister 清理映射）")
    ms_conns.unregister(1)
    check("断开后 push 返回 False", ms_conns.push(UID, b"x") is False)

    # ------------------------------------------------------------ INV-PUSH-4
    print("\n[5] INV-PUSH-4 send_mail 发完自动推送")
    if template.template(TPL) is None:
        print("  SKIP：没有 BDD MailData")
    else:
        ms_conns.reset_for_tests()
        sock2 = FakeSock()
        ms_conns.register(2, sock2)
        ms_conns.bind_uid(2, UID)
        st = fresh()
        mailsend.send_mail(st, TPL)
        check("在线时收到 1 帧", len(sock2.frames) == 1, len(sock2.frames))
        if sock2.frames:
            fp = parse_push(sock2.frames[0])
            check("推的是 MailService.newMailNotice",
                  (fp["service"], fp["method"]) == ("MailService", "newMailNotice"), fp)
            check("目标端点是本账号", fp["ep_ids"] == [UID], fp["ep_ids"])
            got = msmod.mp_decode(fp["params"])[0]
            check("参数是 [mailsInfo] 形状（客户端 unpack 后正好一个实参）",
                  isinstance(got, list) and len(got) == 1 and isinstance(got[0], list), got)
            check("推送里带的就是刚入库那封",
                  got[0] and got[0][0].get("MailId") == store.all_mails(st.props)[0]["MailId"],
                  got[0][:1] if got and got[0] else got)

        # ---------------------------------------------------------- INV-PUSH-5
        print("\n[6] INV-PUSH-5 不在线：发信照常，推送静默跳过")
        ms_conns.reset_for_tests()
        st2 = fresh("push_offline")
        before = len(store.all_mails(st2.props))
        mailsend.send_mail(st2, TPL)
        check("邮件照样入库", len(store.all_mails(st2.props)) == before + 1)
        check("没有在线连接也能不炸（返回 False）", ms_conns.push("nobody", b"x") is False)

    # ------------------------------------------------------------ INV-PUSH-6
    print("\n[7] INV-PUSH-6 msAuth 派生自 uid 且非空")
    st3 = fresh("push_auth")
    check("msAuth 非空", st3.props.get("msAuth") == "oo_push_auth", st3.props.get("msAuth"))
    check("msAuth 属于 derived（不存盘）", prop_origin.origin_of("msAuth") == prop_origin.DERIVED)
    saved = prop_origin.filter_for_save(st3.props)
    check("存档里没有 msAuth（派生不落盘）", "msAuth" not in saved)
    check("重算幂等：再跑一次没有变化", prop_origin.recompute(st3) == {})


    # ------------------------------------------------------------ 并发与重连
    print("\n[8] 并发写同一个 socket：回包与推送不许交错（每帧必须完整）")
    import struct as _struct
    import threading as _th
    import time as _time

    class RecSock:
        """检测"同一时刻是否有两个线程在写这个 socket"。

        ★ 关键在于 sendall 里 **sleep** 一下：CPython 的 GIL 会在 sleep 时释放，
          没有 send_lock 时两个线程一定会在 active>1 处相遇。
          只记 chunk 是测不出来的（list.append 本身原子）。
        """

        def __init__(self):
            self.chunks = []
            self._guard = _th.Lock()
            self.active = 0
            self.overlap = False

        def sendall(self, data):
            with self._guard:
                self.active += 1
                if self.active > 1:
                    self.overlap = True
            _time.sleep(0.0005)          # 让另一个线程有机会挤进来
            with self._guard:
                self.chunks.append(bytes(data))
                self.active -= 1

    ms_conns.reset_for_tests()
    rs = RecSock()
    ms_conns.register(9, rs)
    ms_conns.bind_uid(9, "race_u")
    frame_a = msframes.frame_ms_push("ENTITY", ["race_u"], "S", "m", msmod.mp_arr([[]]))
    frame_b = msmod.frame_ms_response(1, msmod.mp_map({"Ret": [True, ""]}), "S")

    def spam(frame):
        for _ in range(50):
            ms_conns.send_frame(9, frame)

    ts = [_th.Thread(target=spam, args=(frame_a,)),
          _th.Thread(target=spam, args=(frame_b,))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("两线程各发 50 次都落地", len(rs.chunks) == 100, len(rs.chunks))
    check("没有两个线程同时进入 sendall（send_lock 生效）", rs.overlap is False, rs.overlap)
    whole = all(len(c) >= 6 and _struct.unpack("<I", c[:4])[0] == len(c) - 4 for c in rs.chunks)
    check("每一块都是一个完整帧（没有被切开/拼接）", whole, [c[:6] for c in rs.chunks[:3]])

    print("\n[9] 重连顶号：同一个 uid 的新连接接管，旧连接断开不误清")
    ms_conns.reset_for_tests()
    old, new = FakeSock(), FakeSock()
    ms_conns.register(21, old)
    ms_conns.register(22, new)
    ms_conns.bind_uid(21, "dup_u")
    ms_conns.bind_uid(22, "dup_u")
    check("映射指向新连接", ms_conns.cid_of("dup_u") == 22, ms_conns.cid_of("dup_u"))
    ms_conns.unregister(21)                      # 旧连接断开
    check("旧连接断开后映射仍指向新连接", ms_conns.cid_of("dup_u") == 22, ms_conns.cid_of("dup_u"))
    check("推送仍然只到新连接", ms_conns.push("dup_u", b"z") is True and new.frames == [b"z"])
    check("旧连接一帧都没收到", old.frames == [], old.frames)
    ms_conns.unregister(22)
    check("新连接断开后不再可推", ms_conns.push("dup_u", b"z") is False)

    print("\n[10] 开关 OO_MAIL_PUSH=0：不推送，但邮件照常入库（可一键回退）")
    ms_conns.reset_for_tests()
    s10b = FakeSock()
    # ★ 必须绑到**同一个 uid**：否则推送本来就找不到连接，这条用例会假装通过
    OFF_UID = "push_off"
    ms_conns.register(31, s10b)
    ms_conns.bind_uid(31, OFF_UID)
    saved = ooconf.MAIL_PUSH_ENABLED
    try:
        ooconf.MAIL_PUSH_ENABLED = False
        st10 = fresh(OFF_UID)
        before10 = len(store.all_mails(st10.props))
        mailsend.send_mail(st10, TPL)
        check("关掉推送：socket 一帧都没收到", s10b.frames == [], len(s10b.frames))
        check("关掉推送：邮件照样入库", len(store.all_mails(st10.props)) == before10 + 1)
    finally:
        ooconf.MAIL_PUSH_ENABLED = saved

    print("\n[11] unbind：主动解绑后不再推送")
    ms_conns.reset_for_tests()
    s10 = FakeSock()
    ms_conns.register(30, s10)
    ms_conns.bind_uid(30, "unbind_u")
    ms_conns.unbind_cid(30)
    check("解绑后 push 返回 False", ms_conns.push("unbind_u", b"x") is False)
    check("连接本身还在（还能回包）", ms_conns.count() == 1, ms_conns.count())

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
