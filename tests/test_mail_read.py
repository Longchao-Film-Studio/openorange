#!/usr/bin/env python3
"""邮件系统 M1（只读）单元测试。

用法:
    python tests/test_mail_read.py

被测的不变量（docs/方案-邮件系统-全链路复检.md）：

    INV-MAIL-1  邮件存在 props["_oo_mail"]，且**不进 initDict**（_oo_ 前缀的约定）
    INV-MAIL-2  每封邮件 ExpireTime 必须 > 0 且 > CreatedTime
                （客户端 getUnexpiredMailKeyList 用 `ExpireTime > now` 过滤，
                 0 会让邮件在面板里彻底看不见）
    INV-MAIL-3  Params.giftInfo 必须是 **JSON 字符串**（客户端 json.decode 它）
    INV-MAIL-4  字段集合严格等于客户端契约，多一个少一个都不行
    INV-MAIL-5  queryNotification **没有新邮件时不许带 Notification 键**
                （Lua 空表是真值，回 [] 会让红点永远亮）
    INV-MAIL-6  分页 (Start / TotalSize / count) 与客户端 handleOnePageMailList 的公式一致
    INV-MAIL-7  所有写入口都过 write_guard（store 的 add/mark/mark_param/delete/clear_new）
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ 外部副作用先指到临时目录
_TMP = tempfile.mkdtemp(prefix="oo_mail_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import ms as msmod
from game_server import player_state as pstate
from game_server.mail import rpc as mailrpc
from game_server.mail import send as mailsend
from game_server.mail import store, template
from game_server.transport import ms_service as svc

UID = "mail_unit_u"
#: 客户端读得到的字段全集（§4.2）
INSTANCE_KEYS = {"MailId", "Title", "SrcName", "CreatedTime", "ExpireTime", "Params"}
PARAM_KEYS = {"mailTemplateId", "haveRead", "star", "giftReceived", "giftInfo", "linkText", "link"}

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


def fresh_props():
    st = pstate.get_state({}, uid=UID)
    for k in list(st.props):
        if k.startswith("_oo_"):
            del st.props[k]
    return st


def call(method, *args):
    """按客户端的形状发一条 MailService 请求，取解码后的应答。"""
    fields = {4: [msmod.mp_arr(list(args))]}
    return msmod.mp_decode(svc.ms_reply_params("MailService", method, fields, 0))[0]


# ---------------------------------------------------------------- store
def test_store():
    print("\n[1] INV-MAIL-1 存储：_oo_mail + 三个写入口")
    st = fresh_props()
    check("初始没有邮件箱", store.box(st.props) == {})
    check("初始没有新邮件标记", store.has_new(st.props) is False)

    m1 = store.add(st.props, {"Title": "a", "SrcName": "s", "Params": {"haveRead": False}},
                   now=1000)
    check("add 分配 MailId=1", m1["MailId"] == 1, m1["MailId"])
    check("add 之后有邮件箱", isinstance(st.props.get(store.MAIL_KEY), dict))
    check("_oo_ 前缀（不进 initDict 的前提）", store.MAIL_KEY.startswith("_oo_"))
    check("新邮件标记置起", store.has_new(st.props) is True)
    check("CreatedTime 用传入的 now", m1["CreatedTime"] == 1000, m1.get("CreatedTime"))
    check("缺 ExpireTime 时兜底 > 0", m1.get("ExpireTime", 0) > 1000, m1.get("ExpireTime"))

    m2 = store.add(st.props, {"Title": "b", "SrcName": "s", "Params": {}}, now=2000)
    check("MailId 自增", m2["MailId"] == 2, m2["MailId"])
    order = [m["MailId"] for m in store.all_mails(st.props)]
    check("列表按 CreatedTime 倒序", order == [2, 1], order)

    check("mark 改字段", store.mark(st.props, 1, Star=True) is True)
    check("mark 生效", store.get(st.props, 1).get("Star") is True)
    check("mark 不存在的邮件返回 False", store.mark(st.props, 999, Star=True) is False)
    try:
        store.mark(st.props, 1, MailId=7)
    except ValueError:
        check("mark 不许改 MailId", True)
    else:
        check("mark 不许改 MailId", False, "居然改成功了")

    check("mark_param 写 Params", store.mark_param(st.props, 1, "haveRead", True) is True)
    check("mark_param 生效", store.get(st.props, 1)["Params"]["haveRead"] is True)

    check("delete 软删", store.delete(st.props, 1) is True)
    check("软删后不在列表", [m["MailId"] for m in store.all_mails(st.props)] == [2])
    check("软删后数据还在（可查）", store.get(st.props, 1) is not None)
    check("重复 delete 返回 False", store.delete(st.props, 1) is False)

    check("clear_new 之前是 True", store.clear_new(st.props) is True)
    check("clear_new 之后是 False", store.has_new(st.props) is False)
    check("再 clear_new 返回 False", store.clear_new(st.props) is False)


# ---------------------------------------------------------------- template
def test_template():
    print("\n[2] INV-MAIL-2/3 模板 -> 实例（有效期 + giftInfo）")
    if template.template(1) is None:
        print("  SKIP （没有 BDD MailData，跳过模板相关用例）")
        return
    tpl = template.build(1, created=1000)
    check("Title 来自模板", tpl["Title"] == template.template(1).get("title"), tpl["Title"])
    check("SrcName 来自 sender", tpl["SrcName"] == template.template(1).get("sender"), tpl["SrcName"])
    check("ExpireTime > CreatedTime", tpl["ExpireTime"] > tpl["CreatedTime"])
    check("ExpireTime 非零（客户端看不见 0）", tpl["ExpireTime"] != 0)
    check("Params.mailTemplateId 是 int", tpl["Params"]["mailTemplateId"] == 1)
    check("Params.haveRead 初始 False", tpl["Params"]["haveRead"] is False)
    check("Params.giftReceived 初始 False", tpl["Params"]["giftReceived"] is False)

    items = template.attachment_items(template.template(1))
    check("模板 1 有附件", len(items) > 0, items)
    gi = tpl["Params"].get("giftInfo")
    check("giftInfo 是字符串（INV-MAIL-3）", isinstance(gi, str), type(gi))
    import json as _json
    decoded = _json.loads(gi) if isinstance(gi, str) else {}
    check("giftInfo 能解回对象", isinstance(decoded, dict) and len(decoded) == len(items),
          (gi, items))
    check("giftInfo 键是数字字符串",
          all(str(k).lstrip("-").isdigit() for k in decoded), list(decoded))
    check("giftInfo 值等于模板数量",
          {int(k): int(v) for k, v in decoded.items()} == items, (decoded, items))
    check("attachments_of 往返一致", template.attachments_of(tpl) == items)

    tpl2 = template.build(1, created=1000)
    check("两次 build 的 giftInfo 完全一致（排序稳定）", tpl2["Params"]["giftInfo"] == gi)


# ---------------------------------------------------------------- send
def test_send():
    print("\n[3] 发信入口：send_mail 是唯一入口")
    st = fresh_props()
    if template.template(1) is None:
        print("  SKIP （没有 BDD）")
        return
    got = mailsend.send_mail(st, 1, count=3, now=5000)
    check("count=3 发 3 封", len(got) == 3, len(got))
    check("MailId 连续", [m["MailId"] for m in got] == [1, 2, 3], [m["MailId"] for m in got])
    check("都进了邮箱", len(store.all_mails(st.props)) == 3)
    check("未知模板返回空列表（不抛）", mailsend.send_mail(st, 987654321, now=5000) == [])


# ---------------------------------------------------------------- rpc
def test_rpc():
    print("\n[4] INV-MAIL-6 分页 + 契约字段")
    st = fresh_props()
    for i in range(250):
        store.add(st.props, {"Title": f"t{i}", "SrcName": "s", "CreatedTime": 1000 + i,
                             "ExpireTime": 2 ** 31, "Params": {"mailTemplateId": 0,
                                                              "haveRead": False,
                                                              "star": False,
                                                              "giftReceived": False}},
                  now=1000 + i)

    r = call("getUserMailList", UID, 0, 100)
    check("第 1 页成功", r["Ret"][0] is True, r.get("Ret"))
    check("第 1 页 100 封", len(r["MailList"]) == 100, len(r["MailList"]))
    check("TotalSize = 250", r["TotalSize"] == 250, r.get("TotalSize"))
    check("Start 原样回显", r["Start"] == 0, r.get("Start"))

    r2 = call("getUserMailList", UID, 200, 100)
    check("第 3 页 50 封", len(r2["MailList"]) == 50, len(r2["MailList"]))
    check("第 3 页 Start=200", r2["Start"] == 200, r2.get("Start"))

    r3 = call("getUserMailList", UID, 300, 100)
    check("越界页返回空列表", r3["MailList"] == [] and r3["TotalSize"] == 250, r3)

    r4 = call("getUserMailList", UID, 0, 100000)
    check("count 被夹到 MAX_PAGE 以内", len(r4["MailList"]) <= mailrpc.MAX_PAGE, len(r4["MailList"]))

    # 字段契约：每条都要严格等于集合
    first = r["MailList"][0]
    check("实例字段集合正确（INV-MAIL-4）", set(first) <= INSTANCE_KEYS, sorted(first))
    check("必备字段都在", {"MailId", "Title", "SrcName", "CreatedTime", "ExpireTime",
                          "Params"} <= set(first), sorted(first))
    check("Params 字段都在白名单里", set(first["Params"]) <= PARAM_KEYS, sorted(first["Params"]))
    check("每封 ExpireTime > 0（INV-MAIL-2）",
          all(m["ExpireTime"] > 0 for m in r["MailList"]))
    check("每封 ExpireTime > CreatedTime",
          all(m["ExpireTime"] > m["CreatedTime"] for m in r["MailList"]))

    # 整个应答必须能被 msgpack 编码（mp_any 遇到不支持的类型会抛）
    encode_ok, encode_err = True, ""
    try:
        msmod.mp_map({"Ret": [True, ""], "MailList": r["MailList"],
                      "TotalSize": 1, "Start": 0})
    except Exception as e:  # noqa: BLE001
        encode_ok, encode_err = False, f"{type(e).__name__}: {e}"
    check("应答能被 msgpack 编码（任意一封编不进去 = 客户端收到坏包）",
          encode_ok, encode_err)

    print("\n[5] 未知账号 / 不存在的邮件")
    bad = call("getUserMailList", "no_such_uid_xyz", 0, 100)
    check("未知 uid 失败", bad["Ret"][0] is False, bad.get("Ret"))
    bad2 = call("getMailContent", UID, 999999)
    check("不存在的邮件失败", bad2["Ret"][0] is False, bad2.get("Ret"))

    print("\n[6] INV-MAIL-5 红点：没有新邮件时不许带 Notification 键")
    st2 = fresh_props()
    store.add(st2.props, {"Title": "x", "SrcName": "s", "CreatedTime": 1,
                          "ExpireTime": 2 ** 31,
                          "Params": {"mailTemplateId": 0, "haveRead": True,
                                     "star": False, "giftReceived": False}}, now=1)
    q = call("queryNotification", UID)
    check("已读邮件不点亮红点", q["Ret"][0] is True and "Notification" not in q, q)
    check("明确没有 Notification 键（不是空数组）", "Notification" not in q, sorted(q))

    st3 = fresh_props()
    store.add(st3.props, {"Title": "y", "SrcName": "s", "CreatedTime": 1,
                          "ExpireTime": 2 ** 31,
                          "Params": {"mailTemplateId": 0, "haveRead": False,
                                     "star": False, "giftReceived": False}}, now=1)
    q2 = call("queryNotification", UID)
    check("有未读时带 Notification", "Notification" in q2 and len(q2["Notification"]) == 1, q2)
    rd = call("readNotification", UID)
    check("readNotification 成功", rd["Ret"][0] is True, rd)
    q3 = call("queryNotification", UID)
    check("清标记后不再带 Notification", "Notification" not in q3, sorted(q3))


# ---------------------------------------------------------------- guards
def _is_props(node):
    """判断一个表达式是不是 props（含 self.props / st.props）。"""
    import ast
    if isinstance(node, ast.Name):
        return node.id == "props"
    if isinstance(node, ast.Attribute):
        return node.attr == "props"
    return False


def test_guard():
    """INV-MAIL-7：邮件箱的写入口只能有 store 里的那几个。

    ★ 这里**不能**用 PropsWriteError 来验 —— GuardedProps 的设计是「放过 _oo_ 前缀」
      （那是服务端私有数据，客户端看不见，不需要走 Tx）。所以 _oo_mail 就算被别处
      直接赋值也不会报错。真正的防线是**静态检查**：谁在写它、写在哪。
    """
    print("\n[7] INV-MAIL-7 邮件箱的写入口只有 store 里的那几个（静态检查）")
    import ast

    mail_dir = os.path.join(ROOT, "game_server", "mail")
    offenders = []
    writers = {}
    for fn in sorted(os.listdir(mail_dir)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(mail_dir, fn)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, (ast.Assign, ast.AnnAssign, ast.Delete)):
                    continue
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and _is_props(t.value):
                        writers.setdefault(fn, set()).add(node.name)
                        if fn != "store.py":
                            offenders.append(f"{fn}:{node.name}")
    check("只有 store.py 写 props", offenders == [], offenders)
    check("store.py 里写 props 的函数都在白名单内",
          writers.get("store.py", set()) <= {"_ensure", "add", "mark", "mark_param",
                                             "delete", "clear_new"},
          sorted(writers.get("store.py", set())))

    # 全仓（除邮件包自己）不许出现这个字面量 —— 防止有人在业务里直接摸邮件箱
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "game_server")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel.startswith("game_server/mail/"):
                continue
            text = open(path, encoding="utf-8").read()
            if "_oo_mail" in text:
                hits.append(rel)
    check("邮件包以外没有地方直接摸 _oo_mail", hits == [], hits)

    st = fresh_props()
    store.add(st.props, {"Title": "g", "SrcName": "s", "Params": {}}, now=1)
    check("store.mark 在守卫外可用（它自己开守卫）",
          store.mark(st.props, 1, Star=True) is True)


def main():
    print("tmp =", _TMP)
    test_store()
    test_template()
    test_send()
    test_rpc()
    test_guard()

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
