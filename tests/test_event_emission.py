#!/usr/bin/env python3
"""守卫：事件只能从 story/events.py 的构造器发出。

## 为什么需要

事件是任务系统的**唯一驱动源**：apply_event 内部会去比对 TaskEventData，
命中就推进剧情任务 / 通用任务。所以「该发的事件没发」= 任务永远推不动，
而**没有任何报错**。

2026-11-04 的批次 A 就是这样：UseItem_CS / AddCharFavorExp_CS 一直在跑，
但从来没有 apply_event，于是「消耗中浦币」「赠送好感度道具」永远卡住。

另一半是**手拼事件**：dungeon/settle.py 以前写
    GameEvent(_events.EV_FINISH_DUNGEON, ev, ...)
而 ev_dungeon_finish() 定义了却没人调用 —— 构造器里的字段约定
（first_pass 只在首通出现、isWin 的 0/1 形态）在这条真实路径上完全没生效。
两边会各自漂，改一边不会改另一边。

## 三条不变量

    INV-EVENT-1  业务代码不许手拼 GameEvent(EV_*)，必须走 ev_* 构造器
    INV-EVENT-2  每个 ev_* 构造器必须至少有一个调用点（不许有死构造器）
    INV-EVENT-3  每个 EV_* 常量必须至少被一个构造器使用（不许有死常量）

## 破坏性验证

  把 settle.py 改回手拼 GameEvent(EV_FINISH_DUNGEON, ...)   -> [1][2] 红
  删掉 dispatch.py 里的 ev_gift_favor(...) 调用             -> [2] 红
  在 events.py 加一个没人用的 EV_XXX 常量                    -> [3] 红
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "game_server")
EVENTS_FILE = os.path.join(ROOT, "story", "events.py")

_P: list = []
_F: list = []


def check(name, cond, got=None):
    if cond:
        _P.append(name)
        print(f"  OK   {name}")
    else:
        _F.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def _py_files():
    for dirpath, _dirnames, filenames in os.walk(ROOT):
        if "__pycache__" in dirpath:
            continue
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _parse(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read())
    except (OSError, SyntaxError):
        return None


def scan():
    """返回 (手拼点, 构造器调用点)。"""
    hand_built = []
    ctor_calls = []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel == "story/events.py":
            continue
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "GameEvent" and node.args:
                hand_built.append((rel, node.lineno, ast.unparse(node.args[0])))
            elif name and name.startswith("ev_"):
                ctor_calls.append((rel, node.lineno, name))
    return hand_built, ctor_calls


def declared():
    """events.py 里声明的 EV_* 常量和 ev_* 构造器。"""
    from game_server.story import events as ev_mod

    consts = {n: getattr(ev_mod, n) for n in dir(ev_mod)
              if n.startswith("EV_") and isinstance(getattr(ev_mod, n), str)}
    ctors = {n: getattr(ev_mod, n) for n in dir(ev_mod)
             if n.startswith("ev_") and callable(getattr(ev_mod, n))}
    return consts, ctors


def run():
    hand_built, ctor_calls = scan()
    consts, ctors = declared()

    print(f"[0] 扫描到 {len(consts)} 个 EV_* 常量、{len(ctors)} 个 ev_* 构造器")
    check("构造器数量合理（>=10）", len(ctors) >= 10, len(ctors))
    check("常量数量合理（>=10）", len(consts) >= 10, len(consts))

    print()
    print("[1] INV-EVENT-1：业务代码不许手拼 GameEvent(EV_*)")
    for rel, ln, arg in hand_built:
        print(f"       违规 {rel}:{ln}  GameEvent({arg}, ...)")
    check("★ 没有手拼事件（全部走 ev_* 构造器）", not hand_built, hand_built)

    print()
    print("[2] INV-EVENT-2：每个 ev_* 构造器都必须有调用点")
    used = {n for _r, _l, n in ctor_calls}
    dead = sorted(set(ctors) - used)
    for d in dead:
        print(f"       死构造器 {d}()")
    check("★ 每个构造器都被调用过", not dead, dead)
    for name in sorted(ctors):
        hits = [(r, ln) for r, ln, n in ctor_calls if n == name]
        check(f"   {name:22} 调用点 {len(hits)} 处"
              + (f"（{hits[0][0]}:{hits[0][1]}）" if hits else ""),
              bool(hits))

    print()
    print("[3] INV-EVENT-3：每个 EV_* 常量都必须被构造器使用")
    src = open(EVENTS_FILE, encoding="utf-8").read()
    unused = []
    for name, value in sorted(consts.items()):
        rest = src.replace(f"{name} = ", "", 1)
        if name in rest:
            continue
        if f'GameEvent("{value}"' in src:
            continue
        unused.append((name, value))
    for name, value in unused:
        print(f"       死常量 {name} = {value!r}")
    check("★ 没有未使用的 EV_* 常量", not unused, unused)

    print()
    print("[4] ★ 回归：FinishDungeon 必须走构造器（2026-11-06 手拼点）")
    fd_calls = [(r, ln) for r, ln, n in ctor_calls if n == "ev_dungeon_finish"]
    check("ev_dungeon_finish 有调用点（settle.py 已改回构造器）",
          bool(fd_calls), fd_calls)
    if fd_calls:
        check("调用点在 dungeon/settle.py",
              "dungeon/settle.py" in fd_calls[0][0], fd_calls[0][0])
    settle_src = open(os.path.join(ROOT, "dungeon", "settle.py"),
                      encoding="utf-8").read()
    check("★ settle.py 里不再手拼 EV_FINISH_DUNGEON",
          "GameEvent(_events.EV_FINISH_DUNGEON" not in settle_src)

    print()
    print("[5] ★ 回归：批次 A 三个事件必须仍然有发射点")
    for name in ("ev_consume", "ev_gift_favor", "ev_favor_add_level"):
        hits = [(r, ln) for r, ln, n in ctor_calls if n == name]
        check(f"{name} 有发射点", bool(hits), hits)

    print()
    print(f"通过 {len(_P)} 项，失败 {len(_F)} 项")
    if _F:
        print("失败项：" + ", ".join(_F))
    return 0 if not _F else 1


if __name__ == "__main__":
    sys.exit(run())
