#!/usr/bin/env python3
"""一站式自检：改完东西跑这一条，比记 34 个 tools 靠谱。

    python tools/selfcheck.py          # 全跑（含 70 套件单元测试，约 1-2 分钟）
    python tools/selfcheck.py --fast   # 跳过单元测试
    python tools/selfcheck.py --only login

检查项（对应 2026-10-26 收口清单）：
  1. ruff                    代码规范
  2. 单元测试                tests/ 全部套件
  3. 登录演练                每个存档造一次真实登录快照，结构必须合法
                             （防「点登录被踢回登录界面」这类客户端崩溃）
  4. 属性声明自检            spec_data.json 的类型引用全部可解析
  5. RPC 索引一致性          work/rpc_list.tsv（客户端生成）vs 代码里的 RPC_INDEX
                             （防手抄索引漂移 —— 抄错了消息会静默发到错误方法上）

退出码：0 全通过；1 有失败（失败项打印原因尾巴）。
"""
import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OK = "OK"
FAIL = "FAIL"


def _run(cmd, timeout=900):
    """跑子进程，返回 (rc, 合并输出)。"""
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except Exception as e:                      # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _tail(out, n=8):
    """取输出最后几行，失败时打印用。"""
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return "\n    ".join(lines[-n:])


def step_ruff():
    rc, out = _run([sys.executable, "-m", "ruff", "check", "."])
    if rc == 0:
        return OK, "All checks passed"
    return FAIL, _tail(out)


def step_tests():
    rc, out = _run([sys.executable, "-u", "tools/run_all_tests.py"], timeout=1800)
    line = ""
    for l in out.splitlines():
        if l.startswith("suites="):
            line = l.strip()
    if rc == 0:
        return OK, line or "全部通过"
    return FAIL, (line + "\n    " + _tail(out)) if line else _tail(out)


def step_login_drill():
    rc, out = _run([sys.executable, "-u", "tools/login_drill.py", "--all"], timeout=600)
    ok_n = out.count("结构合法")
    if rc == 0:
        return OK, f"{ok_n} 个账号登录快照结构合法"
    return FAIL, _tail(out)


def step_spec():
    """属性声明自检：所有类型引用都能解析。"""
    try:
        from game_server.property import spec
    except Exception as e:                      # noqa: BLE001
        return FAIL, f"spec 加载失败 {type(e).__name__}: {e}"
    problems = []
    props = spec.all_props()
    if len(props) < 200:
        problems.append(f"根属性只有 {len(props)} 个（预期 218）")

    def resolvable(tname, owner):
        if spec.is_simple(tname):
            return True
        if spec.custom(tname) is None:
            problems.append(f"{owner} 引用了未知类型 {tname}")
            return False
        return True

    n_types = 0
    for tname, decl in (spec._load().get("custom_types") or {}).items():   # noqa: SLF001
        n_types += 1
        for fname, fdecl in (decl.get("props") or {}).items():
            ftype = fdecl[0] if isinstance(fdecl, (list, tuple)) else fdecl
            resolvable(ftype, f"{tname}.{fname}")
        vt = decl.get("value_type")
        if vt:
            resolvable(vt, f"{tname}[value]")
    for name in props:
        sp = spec.get(name)
        if sp is None:
            problems.append(f"根属性 {name} 无声明")
            continue
        resolvable(sp.type, name)
    if problems:
        return FAIL, "\n    ".join(problems[:8])
    return OK, f"{len(props)} 个根属性 / {n_types} 个自定义类型，引用全部可解析"


def step_rpc_index():
    """rpc_list.tsv（客户端侧生成）与代码 RPC_INDEX 交叉核对。"""
    from game_server import rpc_index

    tsv = os.path.join(ROOT, "work", "rpc_list.tsv")
    if not os.path.exists(tsv):
        return FAIL, "缺少 work/rpc_list.tsv（先跑 python tools/gen_rpc_list.py）"
    rows = {}
    with open(tsv, encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 3 and cols[1]:
                try:
                    rows[cols[1]] = int(cols[2])
                except ValueError:
                    continue
    if not rows:
        return FAIL, "rpc_list.tsv 里没有解析到任何条目"
    mismatched = []
    checked = 0
    for name, idx in rows.items():
        code_idx = rpc_index.RPC_INDEX.get(name)
        if code_idx is None:
            continue                            # 代码里没有的 C2S 不算错
        checked += 1
        if code_idx != idx:
            mismatched.append(f"{name}: tsv={idx} code={code_idx}")

    # S2C 不在 RPC_INDEX 里（那是 C2S 的索引表），所以单独核对「我们确实会发的 S2C」。
    # 目前只登记在 announce.KINDS；以后新增 S2C 发送点也应当登记进去。
    try:
        from game_server import announce as _announce
        for kind, (s2c_name, s2c_index, _limit) in _announce.KINDS.items():
            tsv_idx = rows.get(s2c_name)
            if tsv_idx is None:
                mismatched.append(f"{s2c_name}（{kind}）不在 rpc_list.tsv")
                continue
            checked += 1
            if tsv_idx != s2c_index:
                mismatched.append(f"{s2c_name}: tsv={tsv_idx} code={s2c_index}")
    except Exception as e:                      # noqa: BLE001
        mismatched.append(f"S2C 核对跳过：{type(e).__name__}: {e}")

    if mismatched:
        return FAIL, "\n    ".join(mismatched[:8])
    return OK, f"{len(rows)} 条客户端 RPC，核对 {checked} 条（C2S + 已登记 S2C），索引一致"


def main():
    ap = argparse.ArgumentParser(description="一站式自检")
    ap.add_argument("--fast", action="store_true", help="跳过单元测试")
    ap.add_argument("--only", help="只跑某一项：ruff/tests/login/spec/rpc")
    args = ap.parse_args()

    steps = [
        ("ruff", "ruff 检查", step_ruff),
        ("tests", "单元测试", step_tests),
        ("login", "登录演练", step_login_drill),
        ("spec", "属性声明自检", step_spec),
        ("rpc", "RPC 索引一致性", step_rpc_index),
    ]
    if args.fast:
        steps = [s for s in steps if s[0] != "tests"]
    if args.only:
        steps = [s for s in steps if s[0] == args.only]
        if not steps:
            print("未知检查项：" + args.only)
            return 1

    width = max(len(label) for _, label, _ in steps)
    results = []
    for i, (_key, label, fn) in enumerate(steps, 1):
        tag, detail = fn()
        results.append((label, tag, detail))
        head = f"[{i}/{len(steps)}] {label:<{width}}  {tag}"
        print(head + ("  " + detail.splitlines()[0] if tag == OK and detail else ""))
        if tag != OK:
            for line in detail.splitlines():
                print("    " + line)

    bad = [r for r in results if r[1] != OK]
    print("-" * 56)
    if bad:
        print("失败： " + "、".join(r[0] for r in bad))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
