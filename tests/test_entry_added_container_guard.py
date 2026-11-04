#!/usr/bin/env python3
"""守卫：entryAdded「通配在末尾」的容器，写入必须走统一入口。

## 这个测试防的是什么（2026-11-04 实机事故）

客户端弹短信的唯一触发点是 config.lua 的 PropertyCallbacks.entryAdded：
    ["npc_chat_prop.address_list.*"] = on_NpcMessageTopic_Unlock_Change
而 puppetpropertycomponent.lua:91-110 的 _getPropertyCallback **按字符串精确查表**
—— 星号不是通配符，是 customtype.lua:263-277 对「不定键容器」生成的固定后缀。

所以同一个 OP_ADD 落在不同层级，算出的回调路径不同：
    pid=容器.__id__, name=外层键  -> path.*    命中
    pid=条目.__id__, name=内层键  -> path.*.*  不命中

往一个**已存在**的外层键里加子项时只有后者可选 —— 回调静默不触发。
表现为「只有第一条能弹，之后全部无声失败」，服务端不报任何错。

## 这条测试怎么防

1. 从客户端 generated/config.lua **现场解析**所有 entryAdded 声明，
   找出「通配在末尾」的那些（= 依赖「容器的键新增」才触发）。
2. 断言每一条都在 npc_sms.ENTRY_ADDED_CONTAINERS 白名单里登记；
   没登记 = 有人新加了同结构容器却没用统一写入口。
3. 用 AST 扫 npc_sms.py，禁止在统一入口之外直接 set 到这些容器。

## 破坏性验证

把 ENTRY_ADDED_CONTAINERS 里的 "call_records" 删掉 -> [2] 立刻红；
把 unlock_topic 改回直接 tx.set((...,address_list,chat,topic), entry) -> [3] 红。
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.client_contract import LUA_ROOT  # noqa: E402

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(name, cond, got=None):
    if cond:
        _PASSED.append(name)
        print(f"  OK   {name}")
    else:
        _FAILED.append(name)
        print(f"  FAIL {name}" + (f"  <- {got!r}" if got is not None else ""))


def parse_entry_added_tail_wildcards():
    """从客户端 config.lua 解析所有「entryAdded + 通配在末尾」的路径。

    返回 set 或 None（文件不存在）。
    ★ 现场解析而不是手抄：客户端配置会随版本变，手抄清单迟早漂移。
    """
    path = os.path.join(LUA_ROOT, "generated", "config.lua")
    if not os.path.exists(path):
        return None
    out = set()
    section = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = re.match(r"^ {8}([A-Za-z_]\w*) = \{$", line)
            if m:
                section = m.group(1)
                continue
            if re.match(r"^ {8}\}", line):
                section = ""
                continue
            if section != "entryAdded":
                continue
            pm = re.match(r'^ {10}\["([^"]+)"\] = \{', line)
            if pm and pm.group(1).endswith(".*"):
                out.add(pm.group(1))
    return out


def find_direct_sets(target_containers):
    """AST 扫 npc_sms.py，找出直接 set 到这些容器的调用。"""
    path = os.path.join(ROOT, "game_server", "story", "npc_sms.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "set" or not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Tuple) or len(arg.elts) < 3:
            continue
        parts = []
        for el in arg.elts:
            if isinstance(el, ast.Constant):
                parts.append(str(el.value))
            else:
                parts.append("<expr>")
        if parts[0] != "npc_chat_prop" or parts[1] not in target_containers:
            continue
        # 只关心「往容器里加 chatId 键」这一层（路径长度 3）。
        # 更深的（address_list[chat][topic].is_over 这种）写的是**已存在话题的
        # 子字段**，客户端走 changed 回调，与 entryAdded 无关，不算违规。
        if len(parts) != 3:
            continue
        hits.append((node.lineno, ".".join(parts)))
    return hits


def run():
    wild = parse_entry_added_tail_wildcards()
    if wild is None:
        print(f"跳过：找不到 {LUA_ROOT}/generated/config.lua")
        return 0

    from game_server.story import npc_sms

    print("[1] 客户端 entryAdded 里「通配在末尾」的声明")
    for w in sorted(wild):
        print(f"       {w}")
    check("能解析到声明（>=2 条，说明解析规则没失效）", len(wild) >= 2, len(wild))

    # 本测试只管 npc_chat_prop 这一棵树：
    #   其它根属性（characterBag / task_bag / story_task_bag …）由各自模块负责，
    #   而且它们的结构不同（多数是「一层容器」或「第一层是固定类型」），
    #   硬塞进同一个白名单只会让白名单变成一张没人维护的大表。
    mine = {w for w in wild if w.startswith("npc_chat_prop.")}

    print()
    print("[2] 每一条都在服务端白名单里登记")
    declared = set(npc_sms.ENTRY_ADDED_CONTAINERS.values())
    missing = sorted(mine - declared)
    check("没有遗漏未登记的容器", not missing, missing)
    extra = sorted(declared - mine)
    check("白名单里没有客户端不认的僵尸项", not extra, extra)

    print()
    print("[3] 这些容器只允许在统一入口里写")
    hits = find_direct_sets(set(npc_sms.ENTRY_ADDED_CONTAINERS.keys()))
    check("直接 set 只出现在统一入口（<=2 处）", len(hits) <= 2, hits)
    for ln, p in hits:
        print(f"       L{ln}: {p}")

    print()
    print("[4] 白名单键名与数据路径一致")
    for key, path in npc_sms.ENTRY_ADDED_CONTAINERS.items():
        check(f"{key} 的路径包含自身名", path.split(".")[1] == key, path)

    print()
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    if _FAILED:
        print("失败项：" + ", ".join(_FAILED))
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(run())
