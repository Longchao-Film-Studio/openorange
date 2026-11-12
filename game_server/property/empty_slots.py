#!/usr/bin/env python3
"""角色「空槽位」的哨兵值 —— 客户端把「没装备」表示成**值为空串**，不是「键不存在」。

## 为什么服务端必须写这个

客户端 SystemConst.EmptyItemUUID = ""（common/systemconst.lua:1124），
而下面这些读点是「先按槽位取，再和空串比较」——**没有 nil 判断**：

    entities/avatarcomponent/avatarsigilcomp.lua:145
        return sigilInfo and sigilInfo[sigilType] ~= SystemConst.EmptyItemUUID

    entities/avatarcomponent/avatarstampcomp.lua:61 / 72 / 122 / 133
        local yinGridID = equipStampInfo[SystemConst.StampType.Yin]
        if yinGridID ~= SystemConst.EmptyItemUUID then ...

键不存在时 nil ~= "" 为**真**，客户端就会当成「这个槽位已经装备了」，
下一步拿 nil 的 gridId 去查道具：

    avatarsigilcomp.lua:435  gridId = self:getCharWearingSigilUUID(charId)   -- nil
    avatarsigilcomp.lua:437  getSigilStrengthLevelLimit(nil)
    avatarsigilcomp.lua:43   return #sigilBreakData                         -- nil 取长度 -> 崩

崩溃点在 RoleDetailV4Panel:setData -> refreshAll -> refreshRedPoint ->
refreshSigilRedPoint 上，表现是**角色详情页点不进去**（面板打开路径中断）。
服务端日志一片正常 —— 这是纯客户端模型差异，只能在客户端真身上验出来。

## 边界（哪些字段要补，哪些不要）

CharacterInfo 里 4 个 StringDictKeyInt 槽位字段（generated/config.lua:1986-2009）：

    sigil_bag            直接按键读（上面两条）            -> 必须补
    stamp_bag            直接按键读（上面两条）            -> 必须补
    equip_bag            只被 pairs() 遍历 + "~= 空串" 判  -> 不需要（补了也无害）
    fashion_equip_bag    直接按键读但走 IsItemUUIDVaild    -> 不需要
                         （avatarbagcomp.lua:927 这个判据自带 nil 判断）

「不需要」的两个不补，理由是**只写模型要求的东西**：多写的每个键都会进存档、
进同步帧、进全量快照，都属于没有依据的行为改变。

## 槽位键从哪来

不手抄。SigilType = {First = 1} / StampType = {Yin = 0, Yang = 1}
都在客户端 common/systemconst.lua 里，这里现读现解析；
解析不到（缺客户端 Lua 目录）时 available() 为 False，调用方跳过并留日志，
由 tests/test_empty_item_slots.py 断言「必须可用」，不让它悄悄失效。
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import callback_contract as _cb

#: 槽位字段 -> 客户端枚举表名（值取自 common/systemconst.lua）
SLOT_ENUMS: dict[str, str] = {
    "sigil_bag": "SigilType",
    "stamp_bag": "StampType",
}

_RE_EMPTY_UUID = re.compile(r'SystemConst\.EmptyItemUUID\s*=\s*"((?:[^"\\]|\\.)*)"')
_RE_ENUM_VALUE = re.compile(r"=\s*(-?\d+)")

#: 解析结果缓存（读一次）：slots / empty / why
_STATE: dict[str, Any] = {}


def _systemconst_path() -> str:
    return os.path.join(_cb.lua_root(), "common", "systemconst.lua")


def _read() -> None:
    if "slots" in _STATE:
        return
    path = _systemconst_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        _STATE.update(slots={}, empty="", why=f"读不到客户端常量表：{path}")
        return

    m = _RE_EMPTY_UUID.search(text)
    if not m:
        _STATE.update(slots={}, empty="",
                      why=f"{path} 里找不到 SystemConst.EmptyItemUUID")
        return
    empty = m.group(1)

    found: dict[str, tuple[int, ...]] = {}
    missing: list[str] = []
    for field, enum in SLOT_ENUMS.items():
        # 形如：SigilType = {First = 1},
        em = re.search(r"\b" + re.escape(enum) + r"\s*=\s*\{([^{}]*)\}", text)
        keys = tuple(sorted(int(v) for v in _RE_ENUM_VALUE.findall(em.group(1)))) if em else ()
        if keys:
            found[field] = keys
        else:
            missing.append(enum)
    if missing:
        _STATE.update(slots={}, empty=empty,
                      why=f"{path} 里找不到枚举：{'、'.join(missing)}")
        return

    _STATE.update(slots=found, empty=empty, why="")


def available() -> bool:
    """能不能读到槽位枚举（读不到时调用方必须跳过，不能瞎补）。"""
    _read()
    return bool(_STATE.get("slots"))


def unavailable_reason() -> str:
    _read()
    return str(_STATE.get("why") or "")


def empty_uuid() -> str:
    """客户端认定的「空槽位」值（= SystemConst.EmptyItemUUID）。"""
    _read()
    return str(_STATE.get("empty") or "")


def slots() -> dict[str, tuple[int, ...]]:
    """槽位字段 -> 必须存在的键集合。"""
    _read()
    return dict(_STATE.get("slots") or {})


def fill(character: dict[Any, Any]) -> int:
    """给一个 CharacterInfo 补齐空槽位哨兵。返回补了几个键。

    幂等：键已存在就一个字都不动 —— 已装备的 UUID 存在同一个键上，
    覆盖会把装备弄丢。
    """
    if not available():
        return 0
    want = empty_uuid()
    n = 0
    for field, keys in slots().items():
        node = character.get(field)
        if not isinstance(node, dict):
            # 字段本身缺属于模板/迁移问题，这里不负责凭空造字段
            continue
        for key in keys:
            if key not in node:
                node[key] = want
                n += 1
    return n


def fill_props(props: dict[Any, Any]) -> int:
    """给 props 里每个角色补齐空槽位（老存档自愈入口）。返回补了几个键。"""
    bag = props.get("characterBag")
    if not isinstance(bag, dict):
        return 0
    n = 0
    for key, character in list(bag.items()):
        if key in ("__tp__", "__id__") or not isinstance(character, dict):
            continue
        n += fill(character)
    return n
