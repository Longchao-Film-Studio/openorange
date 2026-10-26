#!/usr/bin/env python3
"""离线「登录演练」：不启动客户端，造出真实登录快照并校验结构。

## 干什么

走和真实登录**同一条代码路径**（initdict.build_channel_entity -> live_init_dict），
把 createChannelEntity 的字节造出来，解开 initDict，按客户端属性声明校验：
  · 有没有自定义类型节点缺 __id__（客户端会 assert 崩 -> 整号登不上）
  · npc_chat_prop 之类的容器层级有没有被写脏

## 为什么需要

「点登录被踢回登录界面」这类故障，服务端日志是干净的（崩在客户端）。
以前只能让玩家开客户端试。这个工具把「能不能登」变成一条命令。

## 用法

    python tools/login_drill.py --uid 123tst
    python tools/login_drill.py --all

★ 全程在临时目录的存档副本上跑，**不会改你的真实存档**。
"""
import argparse
import os
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read_varint(b, i):
    shift = 0
    val = 0
    while True:
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            return val, i
        shift += 7


def pb_fields(payload):
    """极简 protobuf 解码：只取 length-delimited 字段。"""
    i = 0
    out = {}
    while i < len(payload):
        tag, i = read_varint(payload, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = read_varint(payload, i)
            out[fno] = payload[i:i + ln]
            i += ln
        elif wt == 0:
            out[fno], i = read_varint(payload, i)
        else:
            raise RuntimeError("unexpected wire type " + str(wt))
    return out


def drill_one(uid):
    """返回 (快照字节数, 属性数, 缺 id 列表)。"""
    from game_server.ms import initdict
    from game_server.ms.protocol import mp_decode
    from game_server.property import validate

    pkt = initdict.build_channel_entity(uid=uid)
    raw = pb_fields(pkt[6:])[2]
    snap, _ = mp_decode(raw, 0)
    props = snap.get("__Properties__") if isinstance(snap, dict) else None
    props = props if isinstance(props, dict) else snap
    return len(pkt), len(props), validate.find_missing_ids(props)


def main():
    ap = argparse.ArgumentParser(description="离线校验某个账号的登录快照能否通过客户端解析")
    ap.add_argument("--uid", help="账号名")
    ap.add_argument("--all", action="store_true", help="演练全部存档")
    args = ap.parse_args()

    src = os.environ.get("OO_STATE_DIR") or os.path.join(ROOT, "work", "player_state")
    if args.all:
        uids = sorted(f[:-5] for f in os.listdir(src) if f.endswith(".json"))
    elif args.uid:
        uids = [args.uid]
    else:
        ap.print_help()
        return 1
    if not uids:
        print("没有可演练的存档")
        return 1

    # 副本目录：演练会走「登录初始化」的写状态路径，不能碰真实存档
    tmp = tempfile.mkdtemp(prefix="oo_login_drill_")
    try:
        for fn in os.listdir(src):
            if fn.endswith(".json"):
                shutil.copyfile(os.path.join(src, fn), os.path.join(tmp, fn))
        os.environ["OO_STATE_DIR"] = tmp
        os.environ.setdefault("OO_DB_PATH", os.path.join(tmp, "_drill.sqlite"))

        bad_total = 0
        for uid in uids:
            try:
                size, nprops, miss = drill_one(uid)
            except Exception as e:                  # noqa: BLE001
                print(f"  ✗ {uid}: 演练失败 {type(e).__name__}: {e}")
                bad_total += 1
                continue
            if miss:
                bad_total += 1
                print(f"  ✗ {uid}: 快照 {size}B / {nprops} 个属性，"
                      f"有 {len(miss)} 处缺 __id__（客户端会崩在 ClientAvatar:init）")
                for p, t in miss[:8]:
                    print(f"      - {p} -> {t}")
                if len(miss) > 8:
                    print(f"      …… 还有 {len(miss) - 8} 处")
                print(f"      修法：python tools/fix_missing_ids.py --uid {uid}")
            else:
                print(f"  ✔ {uid}: 快照 {size}B / {nprops} 个属性，结构合法")
        print()
        print("全部合法" if not bad_total else f"{bad_total} 个账号有问题")
        return 0 if not bad_total else 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
