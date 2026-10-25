#!/usr/bin/env python
"""元测试：每个 apply_event 调用点都必须把通用任务同步帧 drain 出去。

★ 为什么有这个测试
  P1 阶段 N-a3 让 apply_event 顺便推进通用任务（task_bag），
  并把产生的同步描述放在 EventResult.task_sync 里。
  **调用点如果忘了 drain，客户端的 task_bag 缓存就会和服务器不一致**：
  进度条不动、任务完成后条目不消失。

  这类「加了新机制但漏改调用点」的 bug 靠人眼查不住（本次已有 9 个调用点），
  所以用元测试把「漏 drain」变成必红。

用法: python tests/test_task_event_sync.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "game_server")

# 允许「不 drain」的文件 + 理由（必须写清楚，不许空着）
ALLOW_NO_DRAIN = {
    os.path.join("ms", "initdict.py"):
        "登录时在快照构建之前跑，播进去/推进的状态直接进 initDict，不需要推帧",
    os.path.join("story", "events.py"):
        "定义处，不是调用点",
}

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def _py_files():
    out = []
    for dirpath, _dirnames, filenames in os.walk(SERVER):
        if "__pycache__" in dirpath:
            continue
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def run():
    del passed[:]
    del failed[:]

    print("\n[1] 扫描 game_server 下所有 apply_event 调用点")
    sites = []
    for p in _py_files():
        rel = os.path.relpath(p, SERVER)
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
        n = len(re.findall(r"apply_event\(", src))
        if n:
            sites.append((rel, n, src))
    check("扫到了 apply_event 调用点（>=5 个文件）", len(sites) >= 5,
          [s[0] for s in sites])

    print("\n[2] 每个调用点都必须 drain（或带理由豁免）")
    missing = []
    exempt = []
    for rel, n, src in sites:
        # ★ 必须看「调用」而不是「出现过」——
        #   导入行也含这个名字（from ... import drain_event_task_sync），
        #   只判 `in src` 会漏掉「导入了但没调」的情况。
        #   这个假阳性是破坏性验证抓出来的（实验3 本应红却绿了）。
        _body = "\n".join(ln for ln in src.splitlines()
                           if not ln.lstrip().startswith(("from ", "import ")))
        has_drain = "drain_event_task_sync(" in _body
        if has_drain:
            continue
        reason = ALLOW_NO_DRAIN.get(rel)
        if reason:
            exempt.append(rel)
        else:
            missing.append((rel, n))
    check("★ 没有漏 drain 的文件", not missing, missing)
    print('      豁免文件：{}'.format(exempt or "无"))

    print("\n[3] 豁免清单不许有「死条目」（文件已不存在或已不调 apply_event）")
    live = {s[0] for s in sites}
    dead = [k for k in ALLOW_NO_DRAIN if k not in live]
    check("★ 豁免清单没有死条目", not dead, dead)

    print("\n[4] EventResult 必须声明 task_hits / task_sync")
    ev = os.path.join(SERVER, "story", "events.py")
    src = open(ev, encoding="utf-8", errors="replace").read()
    check("EventResult.__slots__ 含 task_hits", "task_hits" in src)
    check("EventResult.__slots__ 含 task_sync", "task_sync" in src)
    check("apply_event 里调了 tasks.evaluate_event",
          "_tasks.evaluate_event" in src)

    print("\n[5] drain 辅助必须真的存在")
    from game_server.story.tasks import drain_event_task_sync
    check("drain_event_task_sync 可导入", callable(drain_event_task_sync))
    from game_server.story import events as sev
    r = sev.EventResult()
    check("EventResult 默认 task_sync 是空 list", r.task_sync == [])

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
