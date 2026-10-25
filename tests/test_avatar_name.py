#!/usr/bin/env python
"""P1 阶段 N-a1：取名链路（RpcCheckNickName / RpcChangeNickName / RpcCommitNameGender）。

★ 为什么有这个测试
  这 3 个 RPC 之前全缺，player_name 永远是默认值 "未具名导演"，导致：
    Sign 事件（TaskEventData[1000]）从不发出
    -> TaskData[10]「完成取名」永不完成
    -> TaskData[1]「完成序章」永不进包（PreTask=10）
    -> task_bag[1][1] 永不存在 -> getSystemTaskState(1) 恒 -1
    -> 40 条 unlockType=1 的系统入口数据条件永假
  见 docs/P1-剧情任务状态机.md 阶段 N 的 N2.1 ⑦⑧。

用法: python tests/test_avatar_name.py
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_name_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import avatar  # noqa: E402
from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server import rpc_index  # noqa: E402
from game_server.story import events as sev  # noqa: E402
from tests.frame_guard import guarded_handle, pre_snapshot  # noqa: E402
from tests.shadow_cache import frames_safe  # noqa: E402

passed, failed = [], []
_TAG = ""


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def uid_of(name):
    return "%s_%s" % (name, _TAG)


def fresh(name):
    u = uid_of(name)
    msmod._live_init_dict(u)
    return pstate.get_state(uid=u)


def call(rpc_name, args, rid, uid):
    return guarded_handle(rpc_index.RPC_INDEX[rpc_name], msmod.mp_arr(args),
                          rid, uid=uid)


class _SignCatcher(object):
    """临时替换 events.apply_event，记录发出去的事件。"""

    def __init__(self):
        self.events = []
        self._orig = None

    def __enter__(self):
        self._orig = sev.apply_event
        outer = self

        def fake(st, ev, *a, **kw):
            outer.events.append(ev)
            return outer._orig(st, ev, *a, **kw)

        sev.apply_event = fake
        return self

    def __exit__(self, *a):
        sev.apply_event = self._orig
        return False


def run():
    global _TAG
    _TAG = "%04x" % random.randint(0, 0xFFFF)
    del passed[:]
    del failed[:]

    print("\n[1] 名字校验（纯函数）")
    check("空名字 -> (False, 8002)",
          avatar._validate_name("") == (False, avatar.NAME_INVALID),
          avatar._validate_name(""))
    check("超长名字 -> (False, 8004)",
          avatar._validate_name("x" * (avatar.NAME_MAX_LEN + 1))
          == (False, avatar.NAME_LENGTH))
    check("合法名字 -> (True, 8001)",
          avatar._validate_name("未具名导演") == (True, avatar.NAME_OK),
          avatar._validate_name("未具名导演"))
    check("带控制字符 -> (False, 8002)",
          avatar._validate_name("a\x01b") == (False, avatar.NAME_INVALID))

    print("\n[2] RpcCheckNickName：只校验，不落库")
    st2 = fresh("chk")
    before = st2.props.get("player_name")
    pre2 = pre_snapshot(uid_of("chk"))
    frames2 = call("RpcCheckNickName", ["新名字"], 1, uid_of("chk"))
    check("检查通过不改 player_name", st2.props.get("player_name") == before,
          st2.props.get("player_name"))
    sc2, errs2 = frames_safe(pre2, frames2, "name_check")
    check("检查应答帧过客户端影子缓存", not errs2 and not sc2.warnings,
          (errs2[:1], sc2.warnings[:1]))

    print("\n[3] RpcCommitNameGender：写 player_name + gender + 发 Sign")
    st3 = fresh("commit")
    pre3 = pre_snapshot(uid_of("commit"))
    with _SignCatcher() as cat:
        frames3 = call("RpcCommitNameGender", ["阿光", 2], 2, uid_of("commit"))
    check("player_name 已写入", st3.props.get("player_name") == "阿光",
          st3.props.get("player_name"))
    check("gender 已写入", st3.props.get("gender") == 2,
          st3.props.get("gender"))
    check("★ 发出了 Sign 事件", any(e.type == sev.EV_SIGN for e in cat.events),
          [e.type for e in cat.events])
    sc3, errs3 = frames_safe(pre3, frames3, "name_commit")
    check("取名应答帧过客户端影子缓存", not errs3 and not sc3.warnings,
          (errs3[:1], sc3.warnings[:1]))

    print("\n[4] RpcChangeNickName：改名，但**不**发 Sign")
    st4 = fresh("change")
    st4.props["player_name"] = "旧名字"
    with _SignCatcher() as cat4:
        call("RpcChangeNickName", ["新名字"], 3, uid_of("change"))
    check("player_name 已改", st4.props.get("player_name") == "新名字",
          st4.props.get("player_name"))
    check("★ 改名不发 Sign（Sign 语义是「完成取名」）",
          not any(e.type == sev.EV_SIGN for e in cat4.events),
          [e.type for e in cat4.events])

    print("\n[5] 非法名字不落库")
    st5 = fresh("bad")
    before5 = st5.props.get("player_name")
    call("RpcCommitNameGender", ["", 1], 4, uid_of("bad"))
    check("空名字不写 player_name", st5.props.get("player_name") == before5,
          st5.props.get("player_name"))
    st5b = fresh("bad2")
    before5b = st5b.props.get("player_name")
    call("RpcCommitNameGender", ["x" * 40, 1], 5, uid_of("bad2"))
    check("超长名字不写 player_name",
          st5b.props.get("player_name") == before5b,
          st5b.props.get("player_name"))

    print("\n[6] 名字写入是幂等的")
    st6 = fresh("idem")
    call("RpcCommitNameGender", ["同一个", 1], 6, uid_of("idem"))
    v1 = st6.props.get("player_name")
    call("RpcCommitNameGender", ["同一个", 1], 7, uid_of("idem"))
    check("重复提交同名结果一致", st6.props.get("player_name") == v1,
          (v1, st6.props.get("player_name")))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())