#!/usr/bin/env python3
"""一站式自检：改完东西跑这一条，比记 34 个 tools 靠谱。

    python tools/selfcheck.py          # 全跑（含 70 套件单元测试，约 1-2 分钟）
    python tools/selfcheck.py --fast   # 跳过单元测试
    python tools/selfcheck.py --only login

检查项（对应 2026-10-26 收口清单）：
  1. ruff                    代码规范
  2. mypy                    类型检查（产品代码严格清单见 pyproject.toml）
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


def step_mypy():
    """类型检查（只查产品代码；tests/tools 是脚本，暂不在范围内）。

    目标：game_server / shared / web。新背包模块在 pyproject.toml 里配了严格模式。
    """
    rc, out = _run([sys.executable, "-m", "mypy", "game_server", "shared", "web"], timeout=900)
    if rc == 0:
        line = ""
        for l in out.splitlines():
            if "Success" in l or "no issues" in l:
                line = l.strip()
        return OK, line or "类型检查通过"
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






def step_prop_sync():
    """属性同步一致性：客户端回放帧后的状态必须等于服务端状态（INV-SYNC-3）。

    为什么单独成门：
        实机 bug（点「领取」经验不涨、要重新登录才看到）的根因是
        「服务端改了属性却没发同步帧」。单元测试守的是已知路径，
        这一道守的是所有已实现 RPC回放后都对得上。

    做法：跑每一条 RPC，收帧，用同源 Lua的客户端解析器回放，
        再比对客户端状态与服务端 props。
    """
    rc, out = _run([sys.executable, "-u", "tools/prop_sync_audit.py", "--json"], timeout=900)
    start_at = out.find("{")
    end_at = out.rfind("}") +1
    payload = out[start_at:end_at] if start_at >=0 and end_at > start_at else None
    if payload is None:
        return FAIL, _tail(out,8)
    try:
        import json

        data = json.loads(payload)
    except ValueError as e:
        return FAIL, f"审计输出解析失败: {e}"
    if rc !=0 or data.get("bad"):
        return FAIL, "\n ".join(str(b) for b in data.get("bad", [])[:8])
    return OK, f'审计 {data.get("targets",0)}条 RPC，回放后两端状态一致'

def step_frame_reachability():
    """帧可达性（INV-SYNC-4）：每条帧客户端都必须有接收方。

    为什么和 step_prop_sync 分开成两道门：
        INV-SYNC-3 抓「状态丢了」—— 帧没发、或发错值。
        INV-SYNC-4 抓「状态对但 UI 不刷新」—— 帧发了、值也对，
        但客户端按 optype 选表、用路径精确查表，查不到就静默丢弃。
        2026-11-05 的系统解锁事故只有这一道能抓：
        83 个系统入口解锁后按钮都不出现（要重登），而 INV-SYNC-3 全绿。
    """
    rc, out = _run([sys.executable, "-u", "tools/audit_frame_reachability.py", "--json"], timeout=900)
    start_at = out.find("{")
    end_at = out.rfind("}") + 1
    payload = out[start_at:end_at] if start_at >= 0 and end_at > start_at else None
    if payload is None:
        return FAIL, _tail(out, 8)
    try:
        import json

        data = json.loads(payload)
    except ValueError as e:
        return FAIL, f"审计输出解析失败: {e}"
    summary = data.get("summary") or {}
    bad = data.get("unreachable") or []
    if rc != 0 or bad:
        detail = "\n    ".join(
            f"{b.get('optype')} {'.'.join(b.get('path') or [])} -> key={b.get('key')}"
            for b in bad[:8])
        return FAIL, detail or _tail(out, 8)
    return OK, (f"{summary.get('ops', 0)} 条帧全部可达"
                  f"（{summary.get('driven', 0)} 次驱动产生变更）")


def step_props_writes():
    """属性写入口（INV-PROPS-2）：业务路径上不许有绕过 Tx 的裸写。

    为什么必须单独一道门（2026-11-17 实机事故）：
        street/interact.py 里一句 ia[actor_id] = interact_id 直接改 props，
        被 B 组守卫拒绝 -> 异常 -> gate 关连接 -> 客户端 lose server
        -> 交互上报全部丢失 -> 主线任务 1007 永远停在 Doing。
        单元测试抓不到，因为测试运行器设了 OO_ALLOW_PROPS_WRITE=1 整体关掉了守卫。

    本门用**严格模式**跑（不设 report、不设 bypass）：
    驱动全部 RPC + 剧情/任务/等级/事件/背包 + 街区真实流程，
    只要有一处裸写就会抛出来。
    """
    import json
    import tempfile

    # ★ 用 --json-out 落文件，不从 stdout 抠 JSON：审计驱动真实业务路径时
    #   业务日志会混进 stdout（[sync] / [mail] …），抠出来解析会失败。
    from pathlib import Path

    fd, path = tempfile.mkstemp(prefix="oo_props_audit_", suffix=".json")
    os.close(fd)
    try:
        rc, out = _run([sys.executable, "-u", "tools/audit_props_writes.py",
                        "--strict", "--json-out", path], timeout=1200)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return FAIL, f"审计结果读取失败: {e}"
    finally:
        Path(path).unlink(missing_ok=True)
    raised = data.get("raised") or []
    if rc != 0 or raised:
        detail = "\n    ".join(
            f"{r.get('rpc')}: {r.get('error')}" for r in raised[:8])
        return FAIL, detail or _tail(out, 8)
    return OK, f'{data.get("driven", 0)} 次驱动，业务路径全部走 Tx'


def main():
    ap = argparse.ArgumentParser(description="一站式自检")
    ap.add_argument("--fast", action="store_true", help="跳过单元测试")
    ap.add_argument("--only",
                    help="只跑某一项：ruff/mypy/tests/login/spec/rpc/sync/reach/props")
    args = ap.parse_args()

    steps = [
        ("ruff", "ruff 检查", step_ruff),
        ("mypy", "mypy 类型检查", step_mypy),
        ("tests", "单元测试", step_tests),
        ("login", "登录演练", step_login_drill),
        ("spec", "属性声明自检", step_spec),
        ("rpc", "RPC 索引一致性", step_rpc_index),
        ("sync", "属性同步一致性", step_prop_sync),
        ("reach", "帧可达性", step_frame_reachability),
        ("props", "属性写入口", step_props_writes),
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
