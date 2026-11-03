#!/usr/bin/env python3
"""短信解锁必须发「address_list 层」的 OP_ADD —— 否则客户端不弹窗。

## 这个测试防的是什么（2026-11-04 实机事故）

玩家走到任务 1044「接到杜老爷消息」，短信应该弹出来，但**不弹**。
服务端日志一切正常、任务状态正确、话题也确实写进了存档。

根因（同源 Lua 实测，详见 docs/根因-短信不弹窗-回调路径层级.md）：
  客户端弹手机的**唯一**触发点是 config.lua:8573-8593 的
      AvatarNpcChatComp.PropertyCallbacks.entryAdded
        ["npc_chat_prop.address_list.*"] = on_NpcMessageTopic_Unlock_Change
  而 puppetpropertycomponent.lua:91-110 的 _getPropertyCallback
  **按路径字符串精确查表** —— 星号不是通配符，是 customtype.lua:263-277
  对「不定键容器」生成的固定后缀。

  于是同一个 OP_ADD 落在不同层级，算出的回调路径不同：
      pid=address_list.__id__, name=chatId   -> address_list.*   （命中）
      pid=<record>.__id__,     name=topicId  -> address_list.*.* （不命中）

  旧实现：容器不存在走前者（能弹），容器已存在走后者（静默）。
  表现就是「同一个 chat 下只有第一个话题能弹」。

## 破坏性验证

把 unlock_topic 的 else 分支改回
    tx.set(("npc_chat_prop","address_list",real_chat,topic_id), entry)
下面 [2]~[4] 立刻红。
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="oo_smslvl_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_tmp, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_tmp, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_tmp, "cap"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_server import gamedata  # noqa: E402
from game_server import ms as msmod
from game_server import player_state as ps
from game_server.story import npc_sms  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
OP_ADD = 2


def check(name, cond, got=None):
    if cond:
        PASSED.append(name)
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def _fresh(uid):
    props = msmod.conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    ps.reset_state(props, uid=uid)
    msmod.live_init_dict(uid)
    return ps.get_state(uid=uid)


def _adds(frames):
    """从帧里挑出 OP_ADD，返回 [(propertyId, name)]。"""
    out = []
    for f in frames:
        body = msmod.pb_parse(f[6:])
        op = body.get(3, [0])[0]
        if op != OP_ADD:
            continue
        nm = body.get(4, [b""])[0]
        if isinstance(nm, bytes):
            nm = nm.decode("utf-8", "replace")
        out.append((body.get(2, [0])[0], nm))
    return out


def run():
    # 取同一角色下的三个话题，保证它们落在同一个 chat 里
    same: dict[int, list[int]] = {}
    for tid in range(1000, 2100):
        cfg = gamedata.npc_message(tid)
        if not isinstance(cfg, dict):
            continue
        if int(cfg.get("chatType") or 0) != 1:
            continue
        role = cfg.get("role")
        if role is None:
            continue
        same.setdefault(int(role), []).append(tid)
    groups = [(r, ts) for r, ts in same.items() if len(ts) >= 3]
    if not groups:
        print("跳过：找不到同一角色下 >=3 个 chatType=1 的话题")
        return 0
    role, topics = groups[0]
    topics = sorted(topics)[:3]
    print(f"用 role={role} 的话题 {topics}")
    print()

    st = _fresh("t_sms_level")
    addr_id = st.props["npc_chat_prop"]["address_list"]["__id__"]

    print("[1] 第一次解锁（容器不存在）")
    f1 = npc_sms.unlock_topic(st, role, topics[0], source="t1", allow_unmapped=True)
    a1 = _adds(f1)
    check("有 OP_ADD", len(a1) == 1, a1)
    check("propertyId 是 address_list 的 __id__", a1 and a1[0][0] == addr_id, a1)
    check("name 是 chatId", a1 and a1[0][1] == str(role), a1)

    print()
    print("[2] 第二次解锁（容器已存在 —— 旧实现从这里开始静默失败）")
    f2 = npc_sms.unlock_topic(st, role, topics[1], source="t2", allow_unmapped=True)
    a2 = _adds(f2)
    check("有 OP_ADD", len(a2) == 1, a2)
    check("propertyId 仍是 address_list 的 __id__", a2 and a2[0][0] == addr_id, a2)
    check("name 仍是 chatId（不是 topicId）", a2 and a2[0][1] == str(role), a2)

    print()
    print("[3] 第三次解锁（容器早已存在）")
    f3 = npc_sms.unlock_topic(st, role, topics[2], source="t3", allow_unmapped=True)
    a3 = _adds(f3)
    check("有 OP_ADD", len(a3) == 1, a3)
    check("propertyId 仍是 address_list 的 __id__", a3 and a3[0][0] == addr_id, a3)

    print()
    print("[4] 重发整个会话不能丢旧话题")
    entry = st.props["npc_chat_prop"]["address_list"][role]
    have = sorted(k for k in entry if isinstance(k, int))
    check("三个话题都在", have == sorted(topics), have)

    print()
    print("[5] 幂等：已解锁的话题不再发帧")
    f4 = npc_sms.unlock_topic(st, role, topics[0], source="t4", allow_unmapped=True)
    check("重复解锁返回空帧", f4 == [], _adds(f4))

    print()
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：" + ", ".join(FAILED))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(run())
