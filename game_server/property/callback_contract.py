"""客户端属性回调契约（PropertyCallbacks）—— 服务端侧的「帧可达性」判据。

## 为什么需要这一层

客户端收到属性同步帧后**不会**「收到就刷新 UI」，而是：

1. 按 optype 选回调表：changed / entryAdded / entryDeleted / itemInserted / itemRemoved
2. 用**路径字符串**在表里**精确查表**（puppetpropertycomponent.lua:91-110）

       local callbacks = self.__ClassType.__PropertyCallbacks__[op]
       local callbackInfo = callbacks[callbackName]     -- 直接当 key 查，无通配匹配

3. 查不到 = **静默丢弃**：不报错、不记日志、不重试

所以「帧发出去了」不等于「客户端知道」。路径算错一个 `*`，UI 就不刷新，
而且没有任何报错 —— 只有重新登录（走全量快照，逐键解析，不需要回调）才对。

### 2026-11-05 的导演面板事故就是这一类

解锁系统时服务端发的是 OP_CHANGE：

    服务端 op        : OP_CHANGE  path=(system_p, unlocked_system, 2)
    → 客户端查表 key : system_p.unlocked_system.*
    客户端声明        : entryAdded["system_p.unlocked_system"] / changed["system_p.unlocked_system"]
    → 不命中 → 按钮不出现 → 只有重登才看到

发 OP_ADD 才是对的：key = `system_p.unlocked_system`，命中 entryAdded。

## 路径怎么算（逐行对应客户端 Lua）

    _pathFromRoot2Myself()  (customtype.lua:263-277)
        根属性             -> "root"
        结构体字段 k       -> parent + "." + k
        容器里的自定义对象 -> parent + ".*"

    __OpChange__/__OpDel__ -> _changeSubProperty -> _traceSubNode(k)  (customtype.lua:296-312)
        叶子是结构体字段   -> parent + "." + k
        否则（容器键）     -> parent + ".*"

于是服务端路径 `(root, k1, k2, ...)` 对应的查表 key 是：

    OP_ADD / OP_INSERT / OP_REMOVE : ".".join(segs[:-1])      # 容器自身路径
    OP_CHANGE / OP_DEL             : ".".join(segs)           # 容器路径 + 叶子段

其中 segs 由 describe() 按 spec 类型链推出：结构体字段保留名字，容器键换成 "*"。

## 用法

    from . import callback_contract as _cb
    _cb.resolve(tx.OP_ADD, ("system_p", "unlocked_system", 2))
    # -> ("ClientAvatar", "on_unlocked_system_added")
    _cb.resolve(tx.OP_CHANGE, ("system_p", "unlocked_system", 2))
    # -> None   ← 这一帧客户端收不到

找不到客户端 config.lua 时全部返回 None 并置 `available() is False`，
校验方据此关闭判据（不能因为缺客户端文件就把服务判红）。
"""
from __future__ import annotations

import os
import re
from typing import Any

OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3
OP_INSERT = 4
OP_REMOVE = 5

#: optype -> config.lua 里的回调段名（core/common/propertyparser.lua:8-14）
OP_NAMES = {
    OP_CHANGE: "changed",
    OP_ADD: "entryAdded",
    OP_DEL: "entryDeleted",
    OP_INSERT: "itemInserted",
    OP_REMOVE: "itemRemoved",
}

#: 用容器自身路径查表的 optype（叶子段不参与）。
#:
#: ★ 逐个对着客户端 Lua 核过（puppetpropertycomponent.lua）：
#:     __OpAdd__     :261  path = customObj:_pathFromRoot2Myself()
#:     __OpDel__     :304  path = customObj:_pathFromRoot2Myself()
#:     __OpInsert__  :352  path = customObj:_pathFromRoot2Myself()
#:     __OpRemove__  :395  path = customObj:_pathFromRoot2Myself()
#: 只有 OP_CHANGE 走 _traceSubNode(k)（:135），会多补一段。
#: 所以 DEL 也在这一组 —— 第一版把它归错，把 entryDeleted[item_bag] 判成了
#: entryDeleted[item_bag.*]，当场误报了一条（见 tests/test_callback_contract.py）。
_CONTAINER_OPTS = frozenset((OP_ADD, OP_DEL, OP_INSERT, OP_REMOVE))

_RE_SECTION = re.compile(
    r"^\s*(changed|entryAdded|entryDeleted|itemInserted|itemRemoved)\s*=\s*\{\s*$")
_RE_KEY = re.compile(
    r'^\s*(?:\[\s*"([^"]+)"\s*\]|([A-Za-z_][A-Za-z0-9_]*))\s*=\s*\{\s*$')
_RE_STR = re.compile(r'^\s*"([^"]*)"\s*,?\s*$')
_RE_CLASS = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*$")

#: opname -> {key: (component, callback)}
_DECLS: dict[str, dict[str, tuple[str, str]]] | None = None
#: 根属性名 -> 声明它的组件名集合
_ROOTS: dict[str, set[str]] | None = None
_AVAILABLE = False


def lua_root() -> str:
    """客户端反编译 Lua 的根目录（OO_LUA_ROOT / OO_LUA_DIR 覆盖）。

    与服务端其它「要对齐客户端」的模块共用这一个定位实现 ——
    路径规则抄成两份就会漂移（项目随磁盘换过盘符 F: -> D:）。
    """
    for env_name in ("OO_LUA_ROOT", "OO_LUA_DIR"):
        env = os.environ.get(env_name)
        if env:
            return env
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    for rel in (("..", "archive", "game_data", "LuaAll_Decompiled2"),
                ("archive", "game_data", "LuaAll_Decompiled2")):
        cand = os.path.join(root, *rel)
        if os.path.isdir(cand):
            return cand
    return os.path.join(root, "..", "archive", "game_data",
                        "LuaAll_Decompiled2")


def config_lua_path() -> str:
    """定位客户端的 generated/config.lua（可用 OO_CONFIG_LUA 覆盖）。"""
    explicit = os.environ.get("OO_CONFIG_LUA")
    if explicit:
        return explicit
    return os.path.join(lua_root(), "generated", "config.lua")


def _load() -> None:
    """解析 config.lua 里**所有** Component 的 PropertyCallbacks（不限定哪几个组件）。"""
    global _DECLS, _ROOTS, _AVAILABLE
    if _DECLS is not None:
        return
    _DECLS = {}
    _ROOTS = {}
    path = config_lua_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        _AVAILABLE = False
        return

    n = len(lines)
    i = 0
    while i < n:
        if "PropertyCallbacks" not in lines[i]:
            i += 1
            continue
        # 往回找最近的类名（缩进更浅的 X = {）
        comp = "?"
        base_indent = len(lines[i]) - len(lines[i].lstrip())
        for back in range(i - 1, max(-1, i - 400), -1):
            m = _RE_CLASS.match(lines[back])
            if m:
                ind = len(lines[back]) - len(lines[back].lstrip())
                if ind < base_indent:
                    comp = m.group(1)
                    break
        # 从 PropertyCallbacks = { 开始按花括号配平
        depth = 0
        started = False
        section: str | None = None
        prop: str | None = None
        seen_type = False
        k = i
        while k < n:
            line = lines[k]
            depth += line.count("{") - line.count("}")
            if not started:
                started = depth > 0
                k += 1
                continue
            if depth <= 0:
                break
            sm = _RE_SECTION.match(line)
            if sm:
                section = sm.group(1)
                prop = None
                seen_type = False
                k += 1
                continue
            km = _RE_KEY.match(line)
            if km and section:
                prop = km.group(1) or km.group(2)
                seen_type = False
                k += 1
                continue
            st = _RE_STR.match(line)
            if st and section and prop:
                if not seen_type:
                    seen_type = True          # 第一行是类型，第二行才是回调名
                    k += 1
                    continue
                _DECLS.setdefault(section, {}).setdefault(prop, (comp, st.group(1)))
                _ROOTS.setdefault(prop.split(".")[0], set()).add(comp)
                prop = None
                k += 1
                continue
            k += 1
        i = max(k, i + 1)
    _AVAILABLE = bool(_DECLS)


def available() -> bool:
    """客户端契约表是否可用（缺 config.lua 时为 False，校验方应跳过）。"""
    _load()
    return _AVAILABLE


def declarations() -> dict[str, dict[str, tuple[str, str]]]:
    _load()
    return _DECLS or {}


def roots() -> dict[str, set[str]]:
    _load()
    return _ROOTS or {}


def reload() -> None:
    """丢掉缓存（测试/热更用）。"""
    global _DECLS, _ROOTS, _AVAILABLE
    _DECLS = None
    _ROOTS = None
    _AVAILABLE = False


def describe(path: tuple[Any, ...] | list[Any]) -> tuple[list[str], list[str | None]]:
    """把服务端路径变成 (客户端路径段, 每层类型)。

    段规则与客户端 _pathFromRoot2Myself 一致：
        结构体字段 -> 保留字段名
        容器键     -> "*"
        根         -> 根属性名

    例：("system_p", "unlocked_system", 2)
        -> (["system_p", "unlocked_system", "*"], ["SystemProp", "BooleanDictKeyInt", ...])
    """
    from . import spec as _spec

    path = tuple(path)
    if not path:
        return [], []
    root = str(path[0])
    sp = _spec.get(root)
    cur_type: str | None = sp.type if sp else None
    segs = [root]
    types: list[str | None] = [cur_type]
    for key in path[1:]:
        fields = _spec.struct_fields(cur_type) if cur_type else {}
        if key in fields:
            fdecl = fields[key]
            nxt = fdecl[0] if isinstance(fdecl, (list, tuple)) else fdecl
            segs.append(str(key))
            cur_type = nxt
        else:
            segs.append("*")
            cur_type = _spec.value_type(cur_type) or cur_type
        types.append(cur_type)
    return segs, types


def key_for(optype: int, path: tuple[Any, ...] | list[Any]) -> str:
    """这一帧在客户端会被用哪个 key 去查回调表。"""
    segs, _ = describe(path)
    if not segs:
        return ""
    if optype in _CONTAINER_OPTS:
        return ".".join(segs[:-1]) if len(segs) > 1 else segs[0]
    return ".".join(segs)


def resolve(optype: int, path: tuple[Any, ...] | list[Any]) -> tuple[str, str] | None:
    """解析这一帧在客户端的接收方。

    返回 (component, callback)；客户端收不到时返回 None。
    契约表不可用时返回 None —— 调用方必须先用 available() 判断。
    """
    if not available():
        return None
    name = OP_NAMES.get(optype)
    if not name:
        return None
    table = (declarations().get(name) or {})
    key = key_for(optype, path)
    hit = table.get(key)
    if hit is not None:
        return hit
    # 同一个 key 可能被多个组件声明（union 是后写的覆盖先写的），
    # 这里只关心「有没有接收方」，所以任何一处命中即可。
    return None


def is_reachable(optype: int, path: tuple[Any, ...] | list[Any]) -> bool:
    return resolve(optype, path) is not None


def declared_container(optype: int, path: tuple[Any, ...] | list[Any]) -> bool:
    """客户端在**对应那一段**里，用「不带通配的精确路径」声明过这个容器吗。

    ★ 判据四次收敛的最终形态（2026-11-05），每一条都有实测支撑：

      ① 只看 OP_NAMES[optype] 那一段。
         查 changed 的帧不会因为 entryAdded 有声明就算可达。

      ② 只认「容器自身路径」，而且**路径里不能有 ***。
         这是最关键的一条，来自客户端的回退机制
         （puppetpropertycomponent.lua:150-155）：

             if not swallowed then
               local parentObj = customObj._parent
               self:_queryPropertyChangedCallback(parentObj, customObj._name, ...)
             end

         子属性的 changed 查不到时会**往上一层再查**，所以
         street_info[1001].prestige_level 的 key 虽然是
         street_info.*.prestige_level，回退一步就是 street_info.* ——
         客户端声明 changed["street_info.*"] 正好接住。
         这类「通配声明」是客户端主动要求的兜底，不该判成缺陷。

         反过来，声明成精确路径（不带 *）就意味着**必须精确命中**：
         changed["system_p.unlocked_system"] 期待 key 就是它自己，
         而 OP_CHANGE 算出的是 system_p.unlocked_system.*，永远对不上。

      ③ 回退时客户端还做类型校验（:146-148）：
         声明 customDict，回退传进来的却是 BooleanDictKeyInt -> 只打 error 不调用。
         所以 system_p.unlocked_system 即使回退到 changed 那行也接不住，
         只有 OP_ADD（__OpAdd__ 没有类型校验，:263-271）才真的能调起来。

    ★ 三条实测结论：
        system_p.unlocked_system + OP_CHANGE  容器路径不带星但 key 对不上 -> 报（真 bug）
        street_info.*+ OP_CHANGE              容器路径带星（通配兜底）      -> 不报
        story_task_bag + OP_ADD               entryAdded 只有 story_task_bag.*，
                                              没有 story_task_bag -> 不报
    """
    if not available():
        return False
    name = OP_NAMES.get(optype)
    if not name:
        return False
    table = declarations().get(name) or {}
    segs, _ = describe(path)
    if not segs:
        return False
    container = ".".join(segs[:-1]) if len(segs) > 1 else segs[0]
    if not container or "*" in container:
        return False
    return container in table


def unreachable(ops: list[Any]) -> list[tuple[Any, str]]:
    """挑出「客户端声明了、但这一帧收不到」的帧。返回 [(op, key)]。

    ★ 只报真缺陷：容器在客户端有声明，但这帧的 optype/路径算出的 key 查不到。
      容器完全没声明 = 纯数据属性，合法，跳过。
    """
    if not available():
        return []
    out = []
    for op in ops:
        if is_reachable(op.optype, op.path):
            continue
        if not declared_container(op.optype, op.path):
            continue
        out.append((op, key_for(op.optype, op.path)))
    return out
