#!/usr/bin/env python3
"""守卫：服务端发的属性同步帧，必须能被客户端 PropertyCallbacks 接收。

## 为什么需要

客户端收到帧后不是"直接刷新 UI"，而是：
  1. 按 optype 选回调表（changed / entryAdded / entryDeleted / ...）
  2. 用**路径字符串精确查表**（puppetpropertycomponent.lua:91-110，无通配匹配）
  3. 查不到 = 静默丢弃，不报错、不重试、UI 不刷新

所以「帧发出去了」和「客户端知道了」是两件事，而只有重登
（全量快照，逐键解析，不过回调）才能救回来。

2026-11-05 导演面板事故就这么来的：解锁系统时先在 props 里写值再 tx.set，
被 Tx 判成 OP_CHANGE，客户端拿 system_p.unlocked_system.* 查表查不到，
83 个系统入口解锁后按钮都不出现。

## 判据（不手抄，全部来自客户端）

路径算法与声明表都在 game_server/property/callback_contract.py：
  - 声明表：现场解析 generated/config.lua 的 PropertyCallbacks
  - 路径推算：逐行对应 customtype.lua:263-312

只有一种情况算缺陷：
  客户端在**对应 optype 那一段**里，用「不带通配的精确路径」声明了某个容器，
  而服务端这一帧算出来的查表 key 不是它。

不算缺陷的两种情况（都有实测依据）：
  · 容器根本不在那段里声明 -> 纯数据属性，客户端靠别的属性刷新
  · 容器路径带通配（如 street_info.*）-> 客户端 changed 有向上回退
    （:150-155），子属性查不到会退到这一层接住

## 破坏性验证

  recalc_system_unlocks 改回先写 us[sid]=True 再 tx.set   -> [3] 红
  callback_contract._CONTAINER_OPTS 去掉 OP_DEL            -> [4] 红
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="oo_cbcontract_")
os.environ.setdefault("OO_STATE_DIR", os.path.join(_tmp, "state"))
os.environ.setdefault("OO_DB_PATH", os.path.join(_tmp, "x.db"))
os.environ.setdefault("OO_UNKNOWN_LOG", os.path.join(_tmp, "unknown.tsv"))
os.environ.setdefault("OO_CAPTURE_DIR", os.path.join(_tmp, "cap"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as ps  # noqa: E402
from game_server import story  # noqa: E402
from game_server.property import callback_contract as cb  # noqa: E402
from game_server.property.tx import Tx  # noqa: E402
from game_server.story import rpc as story_rpc  # noqa: E402
from game_server.story.state_machine import create_task, transition  # noqa: E402

_P: list[str] = []
_F: list[str] = []


def check(name, cond, got=None):
    if cond:
        _P.append(name)
        print(f"  OK   {name}")
    else:
        _F.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def fresh(uid):
    props = msmod.conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    ps.reset_state(props, uid=uid)
    msmod.live_init_dict(uid)
    return ps.get_state(uid=uid)


def capture_ops(fn, *a, **kw):
    """跑一段代码，把 Tx.frames() 里产出的 ops 抓出来。"""
    got = []
    orig = Tx.frames

    def spy(self, strict=False):
        got.extend(self.ops)
        return orig(self, strict=strict)

    Tx.frames = spy
    try:
        fn(*a, **kw)
    finally:
        Tx.frames = orig
    return got


def run():
    print("[1] 客户端声明表能解析出来（来自 config.lua，不手抄）")
    check("callback_contract 可用", cb.available(), cb.config_lua_path())
    decls = cb.declarations()
    total = sum(len(v) for v in decls.values())
    check(f"解析到声明（实际 {total} 条）", total > 100, total)
    for sec in ("changed", "entryAdded", "entryDeleted"):
        check(f"{sec} 段非空", bool(decls.get(sec)), len(decls.get(sec) or {}))

    print()
    print("[2] 路径推算与客户端 Lua 一致（三个已知声明反查）")
    cases = [
        # (服务端路径, optype, 期望查表 key)
        (("system_p", "unlocked_system", 2), cb.OP_ADD,
         "system_p.unlocked_system"),
        (("system_p", "unlocked_system", 2), cb.OP_CHANGE,
         "system_p.unlocked_system.*"),
        (("characterBag", 1001, "RuneId"), cb.OP_CHANGE,
         "characterBag.*.RuneId"),
        (("item_bag", "3"), cb.OP_DEL, "item_bag"),
        (("story_task_bag", 1000), cb.OP_CHANGE, "story_task_bag.*"),
    ]
    for path, op, want in cases:
        got = cb.key_for(op, path)
        check(f"{cb.OP_NAMES[op]:11} {'.'.join(str(x) for x in path):34} -> {want}",
              got == want, got)

    print()
    print("[3] ★ 系统解锁帧必须全部可达（2026-11-05 导演面板事故回归）")
    st = fresh("cb_unlock")
    create_task(st, 1005, 1700000000)
    transition(st, 1005, story.TASK_STATE_FINISHED, source="test")
    ops = capture_ops(story_rpc.recalc_system_unlocks, st)
    check("产出了解锁帧", bool(ops), len(ops))
    check("★ 全部是 OP_ADD（不是 OP_CHANGE）",
          all(o.optype == cb.OP_ADD for o in ops),
          [(cb.OP_NAMES[o.optype], o.path) for o in ops][:3])
    bad = cb.unreachable(ops)
    check("★ 全部能被客户端接收", not bad,
          [(cb.OP_NAMES[o.optype], o.path, k) for o, k in bad][:3])
    for o in ops[:1]:
        who = cb.resolve(o.optype, o.path)
        check(f"接收方是 on_unlocked_system_added（实际 {who}）",
              who is not None and who[1] == "on_unlocked_system_added", who)

    print()
    print("[4] OP_DEL 用容器自身路径（item_bag 删除要命中 entryDeleted）")
    check("key_for(OP_DEL, item_bag[x]) == 'item_bag'",
          cb.key_for(cb.OP_DEL, ("item_bag", "3")) == "item_bag",
          cb.key_for(cb.OP_DEL, ("item_bag", "3")))
    check("item_bag 在 entryDeleted 段里被精确声明",
          cb.declared_container(cb.OP_DEL, ("item_bag", "3")))

    print()
    print("[5] 判据不误报：通配声明与纯数据属性都放过")
    check("★ 通配兜底不报（street_info.* 有 changed 声明）",
          not cb.declared_container(cb.OP_CHANGE,
                                   ("street_info", 1001, "prestige_level")),
          cb.key_for(cb.OP_CHANGE, ("street_info", 1001, "prestige_level")))
    check("★ 纯数据属性不报（char_list 任何段都没声明）",
          not cb.declared_container(cb.OP_INSERT, ("char_list", 1)))
    check("★ 根容器 story_task_bag 的 OP_ADD 不报"
          "（entryAdded 只有 story_task_bag.*，服务端算的是 story_task_bag）",
          not cb.declared_container(cb.OP_ADD, ("story_task_bag", 1000)))

    print()
    print("[6] Tx.frames(strict=True) 会把不可达帧变成异常")
    st2 = fresh("cb_tx")
    tx = Tx(st2.props)
    us = st2.props["system_p"]["unlocked_system"]
    # 先直接改 props（错误写法），再 tx.set -> Tx 判成 OP_CHANGE -> 不可达
    us.pop(2, None)
    us[2] = True
    tx.set(("system_p", "unlocked_system", 2), True)
    raised = False
    try:
        tx.frames(strict=True)
    except Exception as e:
        raised = "收不到" in str(e)
    check("★ 错误写法（先改 props 再 tx.set）在 strict 下抛异常", raised)

    print()
    print(f"通过 {len(_P)} 项，失败 {len(_F)} 项")
    if _F:
        print("失败项：" + ", ".join(_F))
    return 0 if not _F else 1


if __name__ == "__main__":
    sys.exit(run())
