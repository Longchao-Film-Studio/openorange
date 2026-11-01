#!/usr/bin/env python3
"""推送实机验收助手：一条命令走完「发信 -> 看服务端有没有推 -> 告诉你下一步看什么」。

用法:
    python tools/mail_push_check.py --uid 123tst
    python tools/mail_push_check.py --uid 123tst --template 1 --wait 90

它在做什么
------------
1. 先确认这个账号有存档（uid = 用户名，不是 account_id，也不是那串数字）
2. 发一封邮件（走 send_mail 唯一入口，所以会自动触发推送）
3. 盯 work/logs/*.log 里最近 N 秒的内容，找三样东西：
     "ClientGate.bind" 相关 / "推送 uid=" / "不在 MS 在线表里"
4. 打印一份「你现在该去客户端日志里找什么」的对照表

★ 为什么要盯日志：推送是服务端主动发的，成败只能在服务端日志（发出去了吗）
  和客户端日志（收到了吗）两头对。这个脚本只管服务端那半。
"""
import argparse
import glob
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from game_server import config as ooconf  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import store, template  # noqa: E402
from game_server.transport import ms_conns  # noqa: E402


def find_logs():
    """work/logs 下的 .log 文件，按修改时间新到旧。"""
    pats = [os.path.join(ooconf.LOG_DIR, "*.log"),
            os.path.join(ooconf.WORK_DIR, "*.log")]
    files = []
    for p in pats:
        files.extend(glob.glob(p))
    return sorted(files, key=os.path.getmtime, reverse=True)


def tail_since(path, since):
    """读日志尾部（文件可能很大，只读最后 2MB）。since 目前只用于调用方筛选文件。"""
    del since                                    # 由调用方用 mtime 过滤，这里只负责读
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()
    except OSError:
        return []


def scan(lines, needles):
    """返回 {needle: [命中的行]}。"""
    out = {n: [] for n in needles}
    for line in lines:
        for n in needles:
            if n in line:
                out[n].append(line.strip())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="推送实机验收助手")
    ap.add_argument("--uid", required=True, help="账号名（= 游戏内 UID，不是 account_id）")
    ap.add_argument("--template", type=int, default=1, help="BDD 模板 id，默认 1")
    ap.add_argument("--wait", type=int, default=60, help="发完后盯日志多少秒，默认 60")
    ap.add_argument("--no-wait", action="store_true", help="不看日志，只发信")
    args = ap.parse_args(argv)

    # ---------------------------------------------------------------- 前置检查
    print("=" * 62)
    print("0) 前置检查")
    print(f"   存档目录 : {ooconf.STATE_DIR}")
    print(f"   日志目录 : {ooconf.LOG_DIR}")
    print(f"   推送开关 : {'开' if ooconf.mail_push_enabled() else '关（OO_MAIL_PUSH=0）'}")

    st = pstate.load(args.uid)
    if st is None:
        print(f"\n   ✗ 没有账号 [{args.uid}] 的存档。")
        print("     这里要填的是**账号名**（username），例如 123tst / aixiaoji / 15900159000；")
        print("     不是 oo_xxx（account_id），也不是那串纯数字。")
        print("     不知道填什么就跑：python -c \"import sqlite3;print([r[0] for r in sqlite3.connect(r'shared/data.db').execute('select username from accounts')])\"")
        return 2
    if template.template(args.template) is None:
        print(f"\n   ✗ BDD 里没有模板 {args.template}（先跑 python tools/mail_send.py --templates）")
        return 2
    tpl = template.template(args.template)
    print(f"   账号有效 : {args.uid}（存档已有 {len(store.all_mails(st.props))} 封邮件）")
    print(f"   模板     : {args.template} 《{tpl.get('title', '')}》")

    # ---------------------------------------------------------------- 连接状态
    print("\n1) 当前 MS 连接（推送要靠它）")
    uids = ms_conns.bound_uids()
    if not uids:
        print("   ⚠ 现在没有任何已 bind 的 MS 连接。")
        print("     服务端日志里如果只有 '[ms] ... OPEN' 而没有 'bind id='，说明客户端没走 bind：")
        print("       - 客户端没进到游戏内（还停在登录界面）")
        print("       - 或者 msAuth 是空的（本次已改成派生 oo_<uid>，重登后应非空）")
    else:
        print(f"   已绑定账号：{uids}")
        if args.uid not in uids:
            print(f"   ⚠ {args.uid} 不在里面 —— 推送会跳过（邮件照样入库，开面板能看到）")

    # ---------------------------------------------------------------- 发信
    t0 = time.time()
    sent = mailsend.send_mail(st, args.template)
    if not sent:
        print("\n✗ 发信失败")
        return 1
    st.save()
    print(f"\n2) 已发 {len(sent)} 封，MailId={[m['MailId'] for m in sent]}（已落盘）")

    # ---------------------------------------------------------------- 看日志
    if args.no_wait:
        _print_expect()
        return 0

    print(f"\n3) 盯服务端日志 {args.wait}s（找推送有没有真的发出去）...")
    needles = ["推送 uid=", "不在 MS 在线表里", "bind id=", "bind 失败", "推送跳过"]
    found = {n: [] for n in needles}
    deadline = time.time() + args.wait
    while time.time() < deadline:
        for path in find_logs()[:5]:
            if os.path.getmtime(path) + 5 < t0:
                continue
            for n, hits in scan(tail_since(path, t0), needles).items():
                for h in hits:
                    if h not in found[n]:
                        found[n].append(h)
        time.sleep(1.0)

    print("\n4) 结果")
    if found["推送 uid="]:
        print("   ✓ 服务端确实把 MicroPush 发出去了：")
        for h in found["推送 uid="][-3:]:
            print(f"       {h}")
    elif found["不在 MS 在线表里"]:
        print("   ✗ 服务端放弃推送：这条账号没有已 bind 的 MS 连接（见第 1 步）")
        for h in found["不在 MS 在线表里"][-2:]:
            print(f"       {h}")
    elif found["推送跳过"]:
        print("   ✗ 推送被跳过（状态里没有 uid 或开关关着）")
    else:
        print("   ⚠ 日志里没看到推送记录。可能原因：")
        print("       - 服务端日志不在 work/logs（看 --help 里的路径）")
        print("       - 服务端没重启，还在跑旧代码")

    _print_expect()
    return 0


def _print_expect():
    print("\n" + "=" * 62)
    print("接下来看**客户端**日志（%LocalLow%/龙潮阁/归龙潮/Player.log），按这条对照：")
    print()
    print("  onPush <uid> received reliable msg           -> ✓ 推送成功，红点应该立刻亮")
    print("  onPush to <uid> for method ... not defined   -> ✗ 订阅者没换成 ClientAvatar")
    print("                                                   （注入脚本 06b_hooks.lua 第(9)段没生效）")
    print("  onPush to <uid> ... target not found         -> ✗ 客户端没 bind / epId 对不上")
    print("  完全没有 onPush 字样                          -> ✗ 客户端没收到帧（服务端那句 '推送 uid=' 也没有？）")
    print()
    print("万一对不上：OO_MAIL_PUSH=0 重启服务端即可止血，邮件功能不受影响。")


if __name__ == "__main__":
    sys.exit(main())
