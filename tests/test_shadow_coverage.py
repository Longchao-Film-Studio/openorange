#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影子缓存接入的元测试（P0 遗留第 3 条）。

遗留原状：30 个测试套件里只有 3 个把服务端推出去的帧喂进客户端影子缓存。

★ 2026-10-22 审计后的第二版。第一版有两个硬伤：
  1. 「有效性」用例直接调 check_frames，测的是 checker 而不是守卫 ——
     把 guarded_handle 改成不校验的版本，元测试仍然全绿。
  2. 结构检查只做字符串匹配，注释/别名都能绕过；阈值 15 也低于实际的 19。
  现在：有效性用例 monkeypatch 一个返回坏帧的 handle 再调 guarded_handle；
  结构检查用 AST 找真正的 Call 节点；覆盖率按「排除元测试自身」的集合断言。

用法: python tests/test_shadow_coverage.py
"""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, player_state as pstate  # noqa: E402
from game_server.property import tx as _tx  # noqa: E402
from tests import frame_guard as fg  # noqa: E402
from tests.frame_guard import check_frames, guarded_handle, pre_snapshot  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


TESTS_DIR = os.path.join(ROOT, "tests")

# 这些文件本身要「造帧 / 喂坏帧」，允许直接调 erpc.handle（白名单带理由）
ALLOW_RAW = {
    "test_shadow_cache.py": "它就是在测影子缓存，必须故意喂各种坏帧",
    "test_shadow_coverage.py": "本文件要 monkeypatch handle 验证守卫",
}


def _test_files():
    return sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py")))


def _call_lines(path):
    """AST 收集 <erpc|entity_rpc>.handle(...) 调用的行号。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "handle":
            v = f.value
            if isinstance(v, ast.Name) and v.id in ("erpc", "entity_rpc"):
                out.append(node.lineno)
    return out


def _uses_guard(path):
    """文件里有没有真正调用 guarded_handle(...)（AST 口径，不是字符串）。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "guarded_handle":
                return True
    return False


def run():
    print("\n[1] 结构保证：测试里不再有裸 erpc.handle 调用（AST 口径）")
    raw = {}
    for p in _test_files():
        name = os.path.basename(p)
        lines = _call_lines(p)
        if lines and name not in ALLOW_RAW:
            raw[name] = lines
    check("没有测试文件绕过守卫直接调 erpc.handle", not raw, raw)
    check("白名单里的文件都写了理由",
          all(isinstance(v, str) and v for v in ALLOW_RAW.values()))
    existing = {os.path.basename(p) for p in _test_files()}
    dead = [k for k in ALLOW_RAW if k not in existing]
    check("白名单没有死条目", not dead, dead)

    print("\n[2] 覆盖率：接了守卫的套件集合")
    guarded = [os.path.basename(p) for p in _test_files()
               if os.path.basename(p) != "test_shadow_coverage.py"
               and _uses_guard(p)]
    print("      已接入 %d 个套件：%s" % (len(guarded), "、".join(guarded)))
    check("接入守卫的套件数 = 19", len(guarded) == 19, len(guarded))
    callers = [os.path.basename(p) for p in _test_files() if _call_lines(p)]
    missing = [c for c in callers
               if c not in ALLOW_RAW and c not in guarded]
    check("所有会调 handle 的套件都接了守卫", not missing, missing)

    print("\n[3] 有效性：守卫本身真的会校验（不是只返回帧）")
    msmod._live_init_dict("shadowcov")
    st = pstate.get_state(uid="shadowcov")
    pre = pre_snapshot("shadowcov")
    check("能取到 RPC 前快照", isinstance(pre, dict)
          and isinstance(pre.get("__Properties__"), dict))
    bag_id = st.props["item_bag"]["__id__"]
    bad = msmod.build_property_sync(
        "", bag_id, _tx.OP_CHANGE, "zz_new_key",
        {"__id__": 999999, "id": "zz_new_key", "itemId": 1, "num": 1,
         "expireTime": 0.0, "getTime": 0.0})
    good = msmod.build_property_sync(
        "", bag_id, _tx.OP_ADD, "zz_new_key",
        {"__id__": 999998, "id": "zz_new_key", "itemId": 1, "num": 1,
         "expireTime": 0.0, "getTime": 0.0})
    raised = False
    try:
        check_frames(pre, [bad], "故意写坏")
    except AssertionError:
        raised = True
    check("check_frames 能抓住坏帧", raised)
    try:
        check_frames(pre, [good], "正确写法")
        check("check_frames 不误报正确帧", True)
    except AssertionError as e:
        check("check_frames 不误报正确帧", False, e)

    # ★ 关键：走 guarded_handle，把底层 handle 换成返回坏帧的假函数
    real_handle = fg._erpc.handle
    try:
        fg._erpc.handle = lambda index, params, rid, uid=None: [bad]
        caught = False
        try:
            guarded_handle(1, b"\x90", 1, uid="shadowcov")
        except AssertionError:
            caught = True
        check("guarded_handle 会校验并抛出坏帧（守卫不是空壳）", caught)
        fg._erpc.handle = lambda index, params, rid, uid=None: [good]
        ok_frame = True
        try:
            guarded_handle(1, b"\x90", 1, uid="shadowcov")
        except AssertionError as e:
            ok_frame = False
            print("      " + str(e)[:100])
        check("guarded_handle 对好帧不误报", ok_frame)
    finally:
        fg._erpc.handle = real_handle

    print("\n[4] pre 为 None 且有属性帧时必须报错（不能静默跳过）")
    ghost = "ghost_uid_never_initialized_%d" % os.getpid()
    real_handle = fg._erpc.handle
    try:
        fg._erpc.handle = lambda index, params, rid, uid=None: [good]
        caught = False
        try:
            guarded_handle(1, b"\x90", 1, uid=ghost)
        except AssertionError:
            caught = True
        check("取不到快照 + 有属性帧 -> 报错而不是空缓存放过", caught)
    finally:
        fg._erpc.handle = real_handle

    print("\n[5] guarded_handle 与 erpc.handle 签名一致")
    from game_server import entity_rpc as erpc
    idx = erpc.RPC_INDEX["SetSignature_CS"]
    frames = guarded_handle(idx, msmod.mp_arr(["shadowcov_sig"]), 7,
                            uid="shadowcov")
    check("guarded_handle 正常返回帧列表", isinstance(frames, list)
          and len(frames) >= 1, frames)

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
