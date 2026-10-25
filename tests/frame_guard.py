#!/usr/bin/env python3
"""测试用的帧安全守卫（P0 遗留第 3 条）。

背景：项目有 30 个测试套件，但之前只有 3 个把服务端推出去的帧喂进
「客户端影子缓存」（tests/shadow_cache.py）验证过。其余套件只断言了
返回值，没有验证「这些帧客户端收到会不会崩」。

用法（把 erpc.handle 换成 guarded_handle 即可）：

    from tests.frame_guard import guarded_handle
    frames = guarded_handle(erpc.RPC_INDEX["RequestSwitchStreet_CS"],
                            msmod.mp_arr([1011, None]), 1, uid="t")

它做三件事：
  1. 调 RPC **之前**取一份无副作用的状态快照（影子缓存的初始态）；
  2. 调 RPC；
  3. 把返回的帧全部喂进影子缓存，客户端会崩就直接抛 AssertionError。

★ 为什么快照必须取在 RPC 之前：审计 P0-3 实测，用 RPC 之后的 initDict
  建缓存时，客户端缓存里已经有那条新数据了，「新键用 OP_CHANGE」这种
  回归**抓不到**。
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from game_server import entity_rpc as _erpc   # noqa: E402
from game_server import player_state as _pstate  # noqa: E402
from game_server.property import sync as _sync  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shadow_cache import frames_safe   # noqa: E402


def pre_snapshot(uid):
    """RPC 之前的状态快照。

    ★ 不是完全无副作用：player_state.peek() 会加载存档（首次）并跑
      id 计数器校正 / 背包键迁移，还会把 is_new 置 False。
      但它**不会**跑登录 tick、脚手架、剧情起点这些会改业务数据的逻辑，
      所以在测试里当「RPC 前快照」用是安全的。
    """
    st = _pstate.peek(uid)
    if st is None:
        return None
    props = {}
    for k, v in st.props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            continue
        props[k] = _sync._strip_private(v, is_root=True)
    return {"__Properties__": props}


def _has_property_sync(frames):
    """这批帧里有没有属性同步帧（type=13）——只有这种帧需要影子缓存校验。"""
    from game_server import ms as _ms
    for f in frames or []:
        if isinstance(f, (bytes, bytearray)) and len(f) >= 6:
            if int.from_bytes(bytes(f[4:6]), "little") == _ms.PROPERTY_SYNC_TYPE:
                return True
    return False


def check_frames(pre, frames, tag):
    """把帧喂进影子缓存；不安全就抛 AssertionError。返回 (sc, errs)。"""
    if not frames:
        return None, []
    sc, errs = frames_safe(pre, frames, tag)
    if errs:
        raise AssertionError("帧不安全(%s)，客户端会崩 %d 处: %s"
                             % (tag, len(errs), errs[:3]))
    return sc, errs


def guarded_handle(index, params, rid, uid=None, tag=None):
    """erpc.handle 的安全版：跑完顺手把帧过一遍客户端影子缓存。

    参数和 erpc.handle 完全一致，所以调用点只要换个函数名。
    """
    pre = pre_snapshot(uid)
    frames = _erpc.handle(index, params, rid, uid=uid)
    if pre is None and _has_property_sync(frames):
        # 取不到快照就没法建客户端缓存 -> 这批属性帧等于没校验。
        # 审计实测：以前这里会静默跳过，等于给了一条「绕过守卫」的后门。
        raise AssertionError(
            "取不到 uid=%r 的 RPC 前快照，但有属性同步帧无法校验（rpc#%s）"
            % (uid, index))
    check_frames(pre, frames, tag or ("rpc#%s" % index))
    return frames
