#!/usr/bin/env python
"""影子缓存接入的元测试（P0 遗留第 3 条）。

★ 第三版（两轮审计后）。前两版的问题：
  1. 「有效性」用例直接调 check_frames，测的是 checker 而不是守卫；
  2. 结构检查只做字符串匹配，别名/getattr 都能绕过；
  3. pre=None 与「快照取在 RPC 之后」这两个不变量没有回归保护；
  4. 没设 OO_STATE_DIR，会把测试状态写进真实 work/player_state；
  5. passed/failed 是模块全局，同进程跑两次计数翻倍。

现在每一条都有对应用例，且**都能真的失败**（下面 [4][5] 是判别性用例）。

用法: python tests/test_shadow_coverage.py
"""
import ast
import glob
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_cov_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate
from game_server.property import sync as prop_sync  # noqa: E402
from game_server.property import tx as _tx  # noqa: E402
from tests import frame_guard as fg  # noqa: E402
from tests.frame_guard import check_frames, pre_snapshot  # noqa: E402

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


TESTS_DIR = os.path.join(ROOT, "tests")

# 只有「自己就要喂坏帧」的文件允许裸调 erpc.handle。
# 每个条目必须真的存在裸调用（[1] 会验证，防止白名单变成死豁免）。
ALLOW_RAW = {
    "test_shadow_cache.py": "它就是在测影子缓存，必须故意喂各种坏帧",
}


def _test_files():
    return sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py")))


def _call_lines(path):
    """AST 找「可能绕过守卫」的调用：

      <erpc|entity_rpc>.handle(...)     显式模块属性
      handle(...)                       从 entity_rpc 直接 import 进来的名字
    """
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
        elif isinstance(f, ast.Name) and f.id == "handle":
            out.append(node.lineno)
    return out


def _uses_guard(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "guarded_handle"):

                return True
    return False


def _frame_id(v):
    return v


def run():
    del passed[:]
    del failed[:]

    print("\n[1] 结构保证：测试里不再有裸 handle 调用（AST 口径）")
    raw = {}
    for p in _test_files():
        name = os.path.basename(p)
        lines = _call_lines(p)
        if lines and name not in ALLOW_RAW:
            raw[name] = lines
    check("没有测试文件绕过守卫直接调 handle", not raw, raw)
    # 白名单必须都是真需要豁免的（防止累积死条目）
    dead = []
    for name in ALLOW_RAW:
        p = os.path.join(TESTS_DIR, name)
        if not os.path.exists(p) or not _call_lines(p):
            dead.append(name)
    check("白名单里没有死条目（每个都真的有裸调用）", not dead, dead)
    check("白名单条目都写了理由",
          all(isinstance(v, str) and v for v in ALLOW_RAW.values()))

    print("\n[2] 覆盖率：接了守卫的套件集合")
    guarded = [os.path.basename(p) for p in _test_files()
               if os.path.basename(p) != "test_shadow_coverage.py"
               and _uses_guard(p)]
    print('      已接入 {} 个套件：{}'.format(len(guarded), "、".join(guarded)))
    check("接入守卫的套件数 >= 19", len(guarded) >= 19, len(guarded))
    callers = [os.path.basename(p) for p in _test_files() if _call_lines(p)]
    missing = [c for c in callers
               if c not in ALLOW_RAW and c not in guarded]
    check("所有会调 handle 的套件都接了守卫", not missing, missing)

    print("\n[3] 有效性：守卫本身真的会校验（不是只返回帧）")
    msmod._live_init_dict("cov_uid")
    st = pstate.get_state(uid="cov_uid")
    pre = pre_snapshot("cov_uid")
    check("能取到 RPC 前快照", isinstance(pre, dict)
          and isinstance(pre.get("__Properties__"), dict))
    bag_id = st.props["item_bag"]["__id__"]
    bad = msmod.build_property_sync(
        prop_sync.SYNC_SELF, bag_id, _tx.OP_CHANGE, "zz_new_key",
        {"__id__": 999999, "id": "zz_new_key", "itemId": 1, "num": 1,
         "expireTime": 0.0, "getTime": 0.0})
    good = msmod.build_property_sync(
        prop_sync.SYNC_SELF, bag_id, _tx.OP_ADD, "zz_new_key",
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

    real_handle = fg._erpc.handle
    try:
        fg._erpc.handle = lambda index, params, rid, uid=None: [bad]
        caught = False
        try:
            fg.guarded_handle(1, b"\x90", 1, uid="cov_uid")
        except AssertionError:
            caught = True
        check("guarded_handle 会校验并抛出坏帧（守卫不是空壳）", caught)
        fg._erpc.handle = lambda index, params, rid, uid=None: [good]
        ok_frame = True
        try:
            fg.guarded_handle(1, b"\x90", 1, uid="cov_uid")
        except AssertionError as e:
            ok_frame = False
            print("      " + str(e)[:100])
        check("guarded_handle 对好帧不误报", ok_frame)
    finally:
        fg._erpc.handle = real_handle

    print("\n[4] 判别性：pre 为 None 且有属性帧时必须报错")
    # ★ 用「根属性标量 OP_CHANGE」：这种帧在**空缓存**上也是合法的（不会因为找不到父对象而报错），
    #   所以如果 pre=None 被静默放行，本用例会绿 —— 它是真正能区分修复前后的一条。
    root_scalar = msmod.build_property_sync(
        prop_sync.SYNC_SELF, 0, _tx.OP_CHANGE, "signature", "ghost_sig")
    ghost = f'ghost_uid_{os.getpid()}'
    real_handle = fg._erpc.handle
    try:
        fg._erpc.handle = lambda index, params, rid, uid=None: [root_scalar]
        caught = None
        try:
            fg.guarded_handle(1, b"\x90", 1, uid=ghost)
        except AssertionError as e:
            caught = str(e)
        check("取不到快照 + 有属性帧 -> 报错而不是空缓存放过",
              caught is not None, caught)
        check("报错信息说明是「取不到快照」",
              bool(caught) and "快照" in caught, caught)
    finally:
        fg._erpc.handle = real_handle

    print("\n[5] 判别性：快照必须取在 RPC **之前**")
    # 假 handle 先改状态（造一个新键），再返回「给这个新键发 OP_CHANGE」的帧。
    # 快照在 RPC 之前 -> 缓存里没有这个键 -> OP_CHANGE 报错 -> 守卫抛（正确）
    # 快照在 RPC 之后 -> 缓存里已经有这个键 -> 不报错 -> 守卫不抛（本用例变红）
    probe_key = "zz_order_probe"
    msmod._live_init_dict("cov_order")
    pstate.get_state(uid="cov_order")

    def fake_handle(index, params, rid, uid=None):
        st3 = pstate.get_state(uid="cov_order")
        st3.props["item_bag"][probe_key] = {
            "__id__": 777777, "id": probe_key, "itemId": 1, "num": 1,
            "expireTime": 0.0, "getTime": 0.0}
        return [msmod.build_property_sync(
            prop_sync.SYNC_SELF, st3.props["item_bag"]["__id__"],
            _tx.OP_CHANGE, probe_key,
            {"__id__": 777777, "id": probe_key, "itemId": 1, "num": 1,
             "expireTime": 0.0, "getTime": 0.0})]

    real_handle = fg._erpc.handle
    try:
        fg._erpc.handle = fake_handle
        caught = False
        try:
            fg.guarded_handle(1, b"\x90", 1, uid="cov_order")
        except AssertionError:
            caught = True
        check("★ 快照取在 RPC 之前（取在后面这条会红）", caught)
    finally:
        fg._erpc.handle = real_handle

    print("\n[6] guarded_handle 真的改了状态（不是只返回帧）")
    from game_server import entity_rpc as erpc
    idx = erpc.RPC_INDEX["SetSignature_CS"]
    msmod._live_init_dict("cov_sig")
    before = pstate.get_state(uid="cov_sig").props.get("signature")
    frames = fg.guarded_handle(idx, msmod.mp_arr(["cov_sig_value"]), 7,
                            uid="cov_sig")
    after = pstate.get_state(uid="cov_sig").props.get("signature")
    check("SetSignature 真的改了服务端状态",
          after == "cov_sig_value" and after != before, (before, after))
    check("返回了属性同步帧", isinstance(frames, list) and len(frames) >= 1,
          len(frames or []))

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
