#!/usr/bin/env python3
"""邮件领奖（M2）测试。

用法:
    python tests/test_mail_gift.py

被测的不变量：

    INV-GIFT-1  领奖必须**先发货、再落盘、最后才回成功**
                （客户端 setMailGiftReceived_cb 拿到 status=true 就本地标已领，
                 假成功 = 奖励永久丢失且无痕）
    INV-GIFT-2  幂等：同一封领两次只发一份；并发领也只发一份
    INV-GIFT-3  发不出去的邮件必须回 fail（不许假成功），已领过的仍然成功
    INV-GIFT-4  领奖走 grants/Tx 唯一发放入口，产生的是**属性同步帧**（不是手工拼的）
    INV-GIFT-5  同源 Lua 回放：把领奖产生的帧喂给客户端解析器，item_bag 与服务端一致
"""
import copy
import os
import sys
import tempfile
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_mailgift_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402

UID = "mail_gift_u"
TPL = 1          # BDD 模板 1：附件 {103: 1000}
ITEM = 103
COUNT = 1000

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
    """重建一个**登录后**的玩家状态（有 item_bag / 钱包 / _oo_ 计数器）。

    ★ 不能用 get_state({}, ...)：空 props 没有 item_bag，发奖入口会直接抛
      BagError（grants.item_bag 把"容器不存在"判成结构损坏）。
      这里走真正的登录快照，并且先删盘上存档，保证每个用例从干净状态开始。
    """
    path = os.path.join(os.environ["OO_STATE_DIR"], uid + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(uid, None)
    msmod.live_init_dict(uid=uid)
    return pstate.STATES[uid]


def claim(*ids):
    fields = {4: [msmod.mp_arr([UID, list(ids)])]}
    return msmod.mp_decode(svc.ms_reply_params("MailService", "setMailGiftReceived",
                                               fields, 0))[0]


def item_count(st, item_id=ITEM):
    """背包里 item_id 的总数。"""
    n = 0
    for grid in (st.props.get("item_bag") or {}).values():
        if isinstance(grid, dict) and int(grid.get("itemId") or 0) == item_id:
            n += int(grid.get("num") or 0)
    return n


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData，无法测附件领取")
        return 0

    # ------------------------------------------------------------ INV-GIFT-1
    print("\n[1] 领取成功：道具进包 + 标记已领 + 落盘")
    st = fresh()
    m = mailsend.send_mail(st, TPL)[0]
    mid = m["MailId"]
    before = item_count(st)
    r = claim(mid)
    check("回成功", r["Ret"][0] is True, r.get("Ret"))
    check(f"道具进包 +{COUNT}", item_count(st) == before + COUNT, item_count(st))
    check("邮件标记已领", store.get(st.props, mid)["Params"]["giftReceived"] is True)
    check("已落盘", os.path.exists(st.path()))

    # ------------------------------------------------------------ INV-GIFT-2
    print("\n[2] 幂等：再领一次不会重复发")
    r2 = claim(mid)
    check("第二次仍回成功（客户端本地已标已领，回 fail 会让 UI 卡住）",
          r2["Ret"][0] is True, r2.get("Ret"))
    check("数量没变", item_count(st) == before + COUNT, item_count(st))

    print("\n[3] 并发：两个线程同时领同一封，只发一份")
    st3 = fresh()
    mid3 = mailsend.send_mail(st3, TPL)[0]["MailId"]
    base3 = item_count(st3)
    errs = []

    def worker():
        try:
            claim(mid3)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("没有线程抛异常", errs == [], errs)
    check("只发了一份", item_count(st3) == base3 + COUNT, item_count(st3))

    # ------------------------------------------------------------ 批量
    print("\n[4] 批量领取：一次领两封")
    st4 = fresh()
    a = mailsend.send_mail(st4, TPL)[0]["MailId"]
    b = mailsend.send_mail(st4, TPL)[0]["MailId"]
    base4 = item_count(st4)
    r4 = claim(a, b)
    check("批量回成功", r4["Ret"][0] is True, r4.get("Ret"))
    check("两份都到账", item_count(st4) == base4 + COUNT * 2, item_count(st4))
    check("两封都标记已领",
          all(store.get(st4.props, i)["Params"]["giftReceived"] for i in (a, b)))

    # ------------------------------------------------------------ INV-GIFT-3
    print("\n[5] 领不到的东西必须回 fail（不许假成功）")
    st5 = fresh()
    m5 = mailsend.send_mail(st5, TPL)[0]["MailId"]
    store.delete(st5.props, m5)
    base5 = item_count(st5)
    r5 = claim(m5)
    check("已删除的邮件 -> fail", r5["Ret"][0] is False, r5.get("Ret"))
    check("没发货", item_count(st5) == base5, item_count(st5))

    r5b = claim(999999)
    check("不存在的邮件 -> fail", r5b["Ret"][0] is False, r5b.get("Ret"))

    print("\n[6] 部分失败：好的那封照领，坏的照 fail")
    st6 = fresh()
    good = mailsend.send_mail(st6, TPL)[0]["MailId"]
    bad = mailsend.send_mail(st6, TPL)[0]["MailId"]
    store.delete(st6.props, bad)
    base6 = item_count(st6)
    r6 = claim(good, bad)
    check("整批回 fail（有一封领不到）", r6["Ret"][0] is False, r6.get("Ret"))
    check("好的那封领到了", item_count(st6) == base6 + COUNT, item_count(st6))
    check("好的那封标记已领", store.get(st6.props, good)["Params"]["giftReceived"] is True)
    check("坏的仍是未领", store.get(st6.props, bad)["Params"]["giftReceived"] is False)
    # 重试坏的那封仍然失败，但好的不会重复发
    r6b = claim(good, bad)
    check("重试仍 fail", r6b["Ret"][0] is False, r6b.get("Ret"))
    check("重试没有重复发", item_count(st6) == base6 + COUNT, item_count(st6))

    print("\n[7] 没有附件的邮件：领了也不该报错（客户端只会对有附件的显示领取按钮）")
    st7 = fresh()
    from game_server.mail import store as _store
    mid7 = _store.add(st7.props, {"Title": "无附件", "SrcName": "s",
                                  "CreatedTime": 1, "ExpireTime": 2 ** 31,
                                  "Params": {"mailTemplateId": 0, "haveRead": False,
                                             "star": False, "giftReceived": False}},
                      now=1)["MailId"]
    r7 = claim(mid7)
    check("回成功", r7["Ret"][0] is True, r7.get("Ret"))
    check("标记已领", _store.get(st7.props, mid7)["Params"]["giftReceived"] is True)

    # ------------------------------------------------------------ INV-GIFT-4
    print("\n[8] 跨通道：领奖产生属性同步帧，且发给**实体连接**")
    from game_server.transport import online as _online
    sent = []
    orig_send_to = _online.send_to

    def fake_send_to(to_uid, frames):
        sent.append((to_uid, list(frames)))
        return (1, 0)

    _online.send_to = fake_send_to
    try:
        st8 = fresh()
        mid8 = mailsend.send_mail(st8, TPL)[0]["MailId"]
        claim(mid8)
    finally:
        _online.send_to = orig_send_to
    check("确实调了 online.send_to", len(sent) == 1, len(sent))
    check("发的是本账号", sent and sent[0][0] == UID, sent[:1])
    frames8 = sent[0][1] if sent else []
    check("有属性同步帧（不是空推送）", len(frames8) > 0, len(frames8))
    from tools.prop_sync_audit import parse_sync_frame
    parsed8 = [parse_sync_frame(f) for f in frames8]
    check("每一帧都是 propertySync2Client", all(p is not None for p in parsed8), parsed8[:2])
    bag_keys = {str(k) for k in (st8.props.get("item_bag") or {})}
    names = [str(p[2]) for p in parsed8 if p]
    check("帧里的键都是 item_bag 里真实存在的格子",
          bool(names) and all(n in bag_keys for n in names), (names, sorted(bag_keys)[:5]))

    # ------------------------------------------------------------ INV-GIFT-5
    print("\n[9] 同源 Lua 回放：客户端回放这些帧后，item_bag 与服务端一致")
    from game_server import ms as _ms
    pstate.STATES.pop("gift_replay", None)
    init = _ms.live_init_dict(uid="gift_replay")
    st9 = pstate.STATES["gift_replay"]
    mid9 = mailsend.send_mail(st9, TPL)[0]["MailId"]

    # 客户端起点 = 登录时真正下发的 initDict（不是 st.props：那里面还有嵌套 __tp__）
    before_props = copy.deepcopy(init["__Properties__"])
    sent9 = []

    def fake_send_to9(_uid, frames):
        sent9.append(list(frames))
        return (1, 0)

    _online.send_to = fake_send_to9
    try:
        fields = {4: [msmod.mp_arr(["gift_replay", [mid9]])]}
        r9 = msmod.mp_decode(svc.ms_reply_params("MailService", "setMailGiftReceived",
                                                 fields, 0))[0]
    finally:
        _online.send_to = orig_send_to
    check("回放场景领取成功", r9["Ret"][0] is True, r9.get("Ret"))

    from tests.lua_prop_replay import PropReplayer
    from tools.prop_sync_audit import diff_states, snapshot_server
    parsed = [p for p in (parse_sync_frame(f) for f in (sent9[0] if sent9 else []))
              if p is not None]
    check("有帧可回放", len(parsed) > 0, len(parsed))
    rp = PropReplayer(before_props)
    rp.apply_frames(parsed)
    client = rp.snapshot()
    server = snapshot_server(st9.props)
    bad = diff_states(server, client)
    check("回放后两端 item_bag 一致（INV-GIFT-5）", bad == [], bad[:5])
    check("客户端回放后确实拿到了道具",
          any(int(g.get("itemId") or 0) == ITEM
              for g in (client.get("item_bag") or {}).values()),
          list((client.get("item_bag") or {}).values())[:2])

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
