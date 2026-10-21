#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子缓存覆盖率审计：跑测试时顺便把每一帧喂进客户端影子缓存，收集不安全的帧。

用法（由 tools/audit_frames_all.py 批量调）：
    OO_FRAME_AUDIT_OUT=xxx.json python -m tools.audit_frames tests.test_street

原理：
  * patch 掉 game_server.entity_rpc.handle，在真调之前取一份**无副作用**的状态快照
    （只读 pstate.peek + 剥私有键，不跑 _live_init_dict，避免改变测试行为）
  * 真调之后把返回的帧全部喂进 shadow_cache.frames_safe
  * 有不安全帧就记一条（哪个测试、哪个 RPC、客户端会崩在哪）
"""
import atexit
import json
import os
import runpy
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_ERRORS = []
_CURRENT = ["(未命名)"]
_STATS = {"rpc": 0, "frames": 0, "checked": 0}


def _pre_snapshot(uid):
    """RPC 之前的状态快照（纯读，不触发任何副作用）。"""
    from game_server import player_state as pstate
    from game_server.property import sync as _sync
    st = pstate.peek(uid)
    if st is None:
        return None
    out = {}
    for k, v in st.props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            continue
        out[k] = _sync._strip_private(v, is_root=True)
    return {"__Properties__": out}


def install():
    """patch erpc.handle，把每帧都过一遍影子缓存。"""
    from game_server import entity_rpc as erpc
    if getattr(erpc, "_frame_audited", False):
        return
    erpc._frame_audited = True
    orig = erpc.handle

    def patched(index, params, rid, uid=None):
        pre = None
        try:
            pre = _pre_snapshot(uid)
        except Exception:
            pre = None
        frames = orig(index, params, rid, uid=uid)
        _STATS["rpc"] += 1
        _STATS["frames"] += len(frames or [])
        if pre is None or not frames:
            return frames
        _STATS["checked"] += 1
        try:
            from shadow_cache import frames_safe
            _sc, errs = frames_safe(pre, frames, "")
        except Exception as e:
            errs = ["frames_safe 自己抛异常: %s" % e]
        if errs:
            _ERRORS.append({
                "test": _CURRENT[0],
                "index": index,
                "rpc": erpc.describe(index),
                "errors": errs[:3],
            })
        return frames

    erpc.handle = patched


def _dump():
    """进程退出时把收集到的错误写成 JSON（子进程模式用）。"""
    out = os.environ.get("OO_FRAME_AUDIT_OUT")
    if not out:
        return
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"stats": _STATS, "errors": _ERRORS}, f,
                      ensure_ascii=False, indent=1)
    except Exception:
        pass


def main(argv):
    if not argv:
        print("用法: python -m tools.audit_frames <测试模块名>")
        return 2
    mod = argv[0]
    tests_dir = os.path.join(ROOT, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    install()
    atexit.register(_dump)
    _CURRENT[0] = mod
    try:
        runpy.run_module(mod, run_name="__main__")
    except SystemExit:
        pass
    except Exception:
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
