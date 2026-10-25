#!/usr/bin/env python
"""仓库级守卫：消灭「先应答、后同步」的写法（R9）。

★ 规则（唯一入口纪律）：
    一个 handler 只要**同时**满足下面两条，就必须走 respond_after_sync：
      1) 直接构造应答帧：调 build_entity_response(...)
      2) 会产出属性同步帧：调 sync_frame / _sync_frame / 某个对象的 .frames()
    （走过入口的函数不会直接调 build_entity_response，所以这条规则天然只挑出违规者。）

★ 为什么这条规则能防住整类问题：
    客户端的帧是按到达顺序同步处理的，entityMessageResponse 一到就执行回调。
    只要有人手写「先应答、后 append 同步」，回调就可能读到旧值，
    症状表现为剧情卡住 / UI 不刷新 / error("服务器和客户端数据不同步")，
    而原因离症状极远（2026-10-23「铁窗泪」查了三层才定位）。
    把写法收口到唯一入口后，这类 bug 在代码里没有表达空间。

★ 如果确有正当理由例外，必须加到 ALLOW 并写清原因（R6），不许静默放过。

用法: python tests/test_frame_order_scan.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "game_server")

# 允许的例外：{"<文件相对路径>:<函数名>": "原因"}
# 目前为空 —— 全部 handler 已收口到 respond_after_sync。
ALLOW = {}

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def call_name(c):
    if isinstance(c.func, ast.Name):
        return c.func.id
    if isinstance(c.func, ast.Attribute):
        return c.func.attr
    return ""


def scan():
    rows = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            src = open(path, encoding="utf-8", errors="replace").read()
            if "build_entity_response" not in src and "respond_after_sync" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError as e:
                rows.append({"rel": os.path.relpath(path, ROOT), "func": "<syntax>",
                             "line": 0, "resp": False, "sync": False,
                             "entry": False, "err": str(e)})
                continue
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                resp = sync = entry = False
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    nm = call_name(sub)
                    if "build_entity_response" in nm:
                        resp = True
                    if "sync_frame" in nm:
                        sync = True
                    if nm == "frames":
                        sync = True
                    if "respond_after_sync" in nm:
                        entry = True
                if not (resp or entry):
                    continue
                rows.append({"rel": rel, "func": node.name, "line": node.lineno,
                             "resp": resp, "sync": sync, "entry": entry, "err": None})
    return rows


def run():
    rows = scan()
    print("\n[1] 扫描 game_server（AST）")
    errs = [r for r in rows if r["err"]]
    check("所有文件都能解析", not errs, [r["rel"] for r in errs])

    offenders = [r for r in rows
                 if r["resp"] and r["sync"] and not r["entry"]
                 and ("{}:{}".format(r["rel"].replace(os.sep, "/"), r["func"])) not in ALLOW]
    entries = [r for r in rows if r["entry"]]
    ack_only = [r for r in rows if r["resp"] and not r["sync"]]

    print("\n[2] 统计")
    print("      走过唯一入口 respond_after_sync 的函数：%d" % len(entries))
    print("      纯 ACK（无同步帧，无需排序）：%d" % len(ack_only))
    print("      违规：%d" % len(offenders))

    print("\n[3] 违规必须为 0")
    check("★ 没有任何 handler 手写「先应答、后同步」", not offenders,
          ["{}:{} (行{})".format(r["rel"], r["func"], r["line"]) for r in offenders])

    print("\n[4] 守卫本身有效：至少覆盖到已知的收口面")
    check("★ 走过入口的函数不少于 20 个", len(entries) >= 20, len(entries))
    check("★ ALLOW 为空或每条都有原因",
          all(bool(v) for v in ALLOW.values()), ALLOW)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
