#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影子缓存接入的元测试（P0 遗留第 3 条）。

遗留原状：30 个测试套件里只有 3 个把服务端推出去的帧喂进客户端影子缓存，
其余只断言返回值 —— 帧在客户端会不会崩，没人验。

现在所有 erpc.handle 调用都换成了 tests/frame_guard.guarded_handle，
这个文件负责三件事：
  1. 结构性保证：任何测试文件都不许再直接调 erpc.handle（会绕过校验）
  2. 有效性保证：守卫**真的能抓到**坏帧（拿一条故意写坏的帧验证它抛异常）
  3. 覆盖率统计：报告有多少套件接了守卫

用法: python tests/test_shadow_coverage.py
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod, player_state as pstate  # noqa: E402
from game_server.property import sync as _sync, tx as _tx  # noqa: E402
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

# 这两个文件本身要造/校验帧，允许直接调 handle
ALLOW_RAW = {"test_shadow_cache.py", "test_shadow_coverage.py",
             "frame_guard.py", "shadow_cache.py"}


def _test_files():
    return sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py")))


def run():
    print("\n[1] 结构保证：测试里不再有裸 erpc.handle 调用")
    raw = []
    for p in _test_files():
        name = os.path.basename(p)
        if name in ALLOW_RAW:
            continue
        src = open(p, encoding="utf-8").read()
        if "erpc.handle(" in src:
            raw.append(name)
    check("没有测试文件绕过守卫直接调 erpc.handle", not raw, raw)

    print("\n[2] 覆盖率：接了守卫的套件数")
    guarded = []
    for p in _test_files():
        src = open(p, encoding="utf-8").read()
        if "guarded_handle(" in src:
            guarded.append(os.path.basename(p))
    print("      已接入 %d 个套件：%s" % (len(guarded), "、".join(guarded)))
    check("至少 15 个套件接入守卫", len(guarded) >= 15, len(guarded))

    print("\n[3] 有效性：守卫真的能抓到坏帧")
    msmod._live_init_dict("shadowcov")
    st = pstate.get_state(uid="shadowcov")
    pre = pre_snapshot("shadowcov")
    check("能取到 RPC 前快照", isinstance(pre, dict)
          and isinstance(pre.get("__Properties__"), dict))
    bag_id = st.props["item_bag"]["__id__"]
    # 故意写坏：给一个**新键**发 OP_CHANGE（客户端 oldVal 为 nil -> 崩）
    bad = msmod.build_property_sync(
        "", bag_id, _tx.OP_CHANGE, "zz_new_key",
        {"__id__": 999999, "id": "zz_new_key", "itemId": 1, "num": 1,
         "expireTime": 0.0, "getTime": 0.0})
    raised = False
    try:
        check_frames(pre, [bad], "故意写坏")
    except AssertionError:
        raised = True
    check("坏帧（新键用 OP_CHANGE）被守卫抓住", raised)

    # 同一条数据用 OP_ADD 就应该通过
    good = msmod.build_property_sync(
        "", bag_id, _tx.OP_ADD, "zz_new_key",
        {"__id__": 999998, "id": "zz_new_key", "itemId": 1, "num": 1,
         "expireTime": 0.0, "getTime": 0.0})
    ok_raised = True
    try:
        check_frames(pre, [good], "正确写法")
        ok_raised = False
    except AssertionError:
        ok_raised = True
    check("同样的数据用 OP_ADD 不会被误报", not ok_raised)

    print("\n[4] guarded_handle 与 erpc.handle 签名一致")
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
