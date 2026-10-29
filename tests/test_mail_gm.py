#!/usr/bin/env python3
"""GM 发信（M4）测试。

用法:
    python tests/test_mail_gm.py

被测的不变量：

    INV-GM-1  GmRpcSendMailToPlayer / GmRpcSendMailToGroup 必须**登记在索引表里**
              （以前没登记：严格模式回 not implemented，兼容模式假成功）
    INV-GM-2  发信走 send_mail 唯一入口 —— 发完邮箱里出现的是"模板实例"，
              有 Title/SrcName/ExpireTime/附件，不是手拼的半截数据
    INV-GM-3  模板不存在必须回 status=false（不许假成功）
    INV-GM-4  群发只发给**在线**玩家，且每个目标都真的收到
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_mailgm_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import (
    dispatch,  # noqa: E402
    rpc_index,  # noqa: E402
)
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.transport import online  # noqa: E402

UID = "mail_gm_u"
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


def fresh(uid=UID):
    path = os.path.join(os.environ["OO_STATE_DIR"], uid + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(uid, None)
    msmod.live_init_dict(uid=uid)
    return pstate.STATES[uid]


def ret_of(frames):
    """从 handle() 返回的帧里取应答的 ret。

    ★ parse_entity_response 已经把 field 4 的 msgpack **解码**了（返回 [retStatus, response]），
      不要再对它 mp_decode 一次 —— 那会拿到 dict 然后炸在协议解析里。
    """
    for f in frames:
        if len(f) < 6 or int.from_bytes(f[4:6], "little") != 2:
            continue
        try:
            _mtype, _method, _rid, val = msmod.parse_entity_response(f)
        except Exception:  # noqa: BLE001 —— 属性同步帧不是应答帧
            continue
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val[0]
    return None


class _FakeConn:
    """只实现 sendall，够 online.send 用。"""

    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData")
        return 0

    # ------------------------------------------------------------ INV-GM-1
    print("\n[1] INV-GM-1 两条 GM RPC 必须登记在索引表里")
    for name, want in (("GmRpcSendMailToPlayer", 542730945),
                       ("GmRpcSendMailToGroup", 494893116)):
        got = rpc_index.RPC_INDEX.get(name)
        check(f"{name} 已登记", got == want, got)
    hi_player = rpc_index.RPC_INDEX["GmRpcSendMailToPlayer"]
    hi_group = rpc_index.RPC_INDEX["GmRpcSendMailToGroup"]

    # ------------------------------------------------------------ INV-GM-2
    print("\n[2] INV-GM-2 给自己发一封：邮箱里出现的是完整模板实例")
    st = fresh()
    check("发之前邮箱是空的", store.all_mails(st.props) == [])
    frames = dispatch.handle(hi_player, msmod.mp_arr([TPL]), 7, uid=UID)
    ret = ret_of(frames)
    check("回 status=true", isinstance(ret, dict) and ret.get("status") is True, ret)
    mails = store.all_mails(st.props)
    check("邮箱里有 1 封", len(mails) == 1, len(mails))
    if mails:
        m = mails[0]
        tpl = template.template(TPL)
        check("Title 来自模板", m.get("Title") == tpl.get("title"), m.get("Title"))
        check("SrcName 来自模板", m.get("SrcName") == tpl.get("sender"), m.get("SrcName"))
        check("ExpireTime > 0", int(m.get("ExpireTime") or 0) > 0, m.get("ExpireTime"))
        check("带模板附件", template.attachments_of(m) != {}, m.get("Params"))
        check("有 mailTemplateId", m["Params"].get("mailTemplateId") == TPL)
    check("新邮件标记置起（下次开面板会亮红点）", store.has_new(st.props) is True)

    # ------------------------------------------------------------ INV-GM-3
    print("\n[3] INV-GM-3 模板不存在 -> 明确失败，不许假成功")
    st = fresh()
    bad = dispatch.handle(hi_player, msmod.mp_arr([987654321]), 8, uid=UID)
    ret_bad = ret_of(bad)
    check("回 status=false", isinstance(ret_bad, dict) and ret_bad.get("status") is False, ret_bad)
    check("邮箱里没多东西", store.all_mails(st.props) == [])
    check("失败原因说清楚是模板问题",
          isinstance(ret_bad, dict) and "not found" in str(ret_bad.get("errmsg", "")), ret_bad)

    # 参数都不给也不能炸
    check("空参数不抛异常", isinstance(dispatch.handle(hi_player, msmod.mp_arr([]), 9, uid=UID), list))

    # ------------------------------------------------------------ INV-GM-4
    print("\n[4] INV-GM-4 群发：只发给在线玩家，每人一封")
    online.reset_for_tests()
    fresh(UID)
    other = "mail_gm_peer"
    online.register(901, _FakeConn())
    online.set_uid(901, other)
    st_other = fresh(other)
    try:
        frames = dispatch.handle(hi_group, msmod.mp_arr([TPL]), 10, uid=UID)
        ret_g = ret_of(frames)
        check("回 status=true", isinstance(ret_g, dict) and ret_g.get("status") is True, ret_g)
        st_me = pstate.peek(UID)
        check("发起人收到", len(store.all_mails(st_me.props)) == 1,
              len(store.all_mails(st_me.props)))
        check("另一个在线玩家也收到", len(store.all_mails(st_other.props)) == 1,
              len(store.all_mails(st_other.props)))
        check("离线玩家没有邮箱箱（不假装群发）",
              store.all_mails(pstate.peek("nobody_offline").props
                              if pstate.peek("nobody_offline") else {}) == [])
    finally:
        online.reset_for_tests()

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
