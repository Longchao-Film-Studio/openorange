#!/usr/bin/env python3
"""tools/check_story_probe.py —— 实机测试后一键看结果。

用法（客户端测完关掉后跑）：
    python tools/check_story_probe.py

它做三件事：
  1. 从 launcher/dist/oo_internal.log 里抠出 storycb| 钩子输出
  2. 检查服务端是否记录了剧情任务推进（story / task / dungeon 日志）
  3. 给出通过 / 不通过 / 证据不足 的结论

为什么需要它：实机测试的证据全在日志里，人眼翻 4MB 日志不现实。
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "launcher", "dist", "oo_internal.log")


def read_log(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    print("=" * 60)
    print("剧情系统实机测试结果")
    print("=" * 60)
    text = read_log(LOG)
    if text is None:
        print("找不到注入日志：{}".format(LOG))
        return 2
    lines = text.split(chr(10))
    print('日志行数：{}'.format(len(lines)))

    # 1) storycb 钩子
    cb_lines = [l for l in lines if "storycb|" in l]
    print()
    print("[1] 客户端回调钩子（onStoryTaskItemChanged）")
    if not cb_lines:
        print("    [X] 日志里没有任何 storycb| 行")
        print("      可能原因：")
        print("        - 用的还是旧 DLL（要重开启动器才会加载新探针）")
        print("        - 客户端没进到街区（钩子在街区阶段才装）")
    else:
        installed = [l for l in cb_lines if "钩子已安装" in l]
        hits = [l for l in cb_lines if "次触发" in l]
        reports = [l for l in cb_lines if "报告：" in l]
        print('    钩子安装：{}'.format("[OK]" if installed else "[X]"))
        print('    触发次数：{} 条触发日志'.format(len(hits)))
        for l in hits[:8]:
            print("      " + l.split("storycb|", 1)[1].strip()[:100])
        print("    汇报行：")
        for l in reports[:10]:
            print("      " + l.split("storycb|", 1)[1].strip()[:100])
        if hits:
            print()
            print("    * 结论：客户端回调**真的被触发了** —— 这是 A6/B6 一直未证的那一项")
        elif installed:
            print()
            print("    [?] 钩子装上了但一次都没触发（可能是这轮没有任何任务状态变化）")

    # 2) 服务端剧情推进痕迹
    print()
    print("[2] 服务端剧情推进痕迹")
    pats = [
        ("任务完成", "完成剧情任务"),
        ("任务归档", "归档剧情任务"),
        ("解锁系统", "解锁系统"),
        ("副本进", "进副本"),
        ("SpaceTask", "SpaceTask"),
        ("剧情播种", "播种"),
    ]
    found_any = False
    for name, kw in pats:
        n = sum(1 for l in lines if kw in l)
        if n:
            found_any = True
            print('    {:<12} {:d} 条'.format(name, n))
    if not found_any:
        print("    日志里没有服务端剧情日志（注入日志只记客户端，服务端日志在控制台）")

    # 3) 结论
    print()
    print("=" * 60)
    if cb_lines and any("次触发" in l for l in cb_lines):
        print("结论：通过 —— 客户端回调链路已验证")
        return 0
    if cb_lines:
        print("结论：部分 —— 钩子装上了但没触发，需要做一次任务状态变化再测")
        return 1
    print("结论：证据不足 —— 没看到 storycb 钩子输出")
    print("        请确认：1) 重开启动器（加载新 DLL） 2) 登录后进到街区")
    return 1


if __name__ == "__main__":
    sys.exit(main())