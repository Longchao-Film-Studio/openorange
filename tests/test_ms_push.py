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

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import prop_origin  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.ms import frames as msframes  # noqa: E402
from game_server.transport import ms_conns  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402

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


def parse_push(frame):
    """把一帧 MicroPush 解回 (org_type, ep_ids, msg_uuid, service, method, params)。"""
    mtype = int.from_bytes(frame[4:6], "little")
    body = msmod.pb_parse(frame[6:])
    return {
        "type": mtype,
        "service": body.get(1, [b""])[0].decode() if body.get(1) else "",
        "method": body.get(2, [b""])[0].decode() if body.get(2) else "",
        "params": body.get(3, [b""])[0] if body.get(3) else b"",
        "msg_uuid": body.get(4, [b""])[0].decode() if body.get(4) else "",
        "ep_ids": [b.decode() for b in body.get(5, [])],
        "org_type": body.get(6, [b""])[0].decode() if body.get(6) else "",
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

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
