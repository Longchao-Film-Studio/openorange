#!/usr/bin/env python3
"""按客户端类型声明建自定义类型实例 —— 唯一入口。

数据来源
--------
spec_data.json（由 tools/gen_property_spec.py 从客户端 generated/config.lua
机械生成，345 个 custom type）。**这里不手抄任何字段表。**

为什么需要它
------------
客户端收到自定义类型实例时按声明逐个字段做 roinit：
  - 简单类型必须有值（漏了就 nil）
  - 结构体 / 容器必须带 __id__（漏了客户端 roinit 崩）

以前这些"形状"在 6 个地方各抄一份：scaffold / story/state_machine /
story/__init__ / tasks / street/info / street/dispatch。抄错或客户端改表
不同步就崩，而且新增一个类型（如抽卡给新角色）还得再抄一份。

现在统一从这里按声明生成，调用方只负责填业务值。

__tp__ 规则（重要，踩过）
------------------------
- 顶层对象 / 根属性值：**带** __tp__（客户端靠它找类型）
- 嵌套对象：**只带 __id__，不带 __tp__**
  多带 __tp__ 会让 CustomDict 解析走错分支，实测把客户端踢回登录页。

★ 本模块**不写 state**：剧情/任务的状态迁移仍由 story.state_machine /
  tasks 的白名单函数负责（那边有 AST 守卫测试）。工厂只造壳。
"""
from . import ids as _ids
from . import spec


def _decl_type(decl):
    """spec 的字段声明形如 [类型, 默认值]。"""
    if isinstance(decl, (list, tuple)):
        return decl[0] if decl else None
    if isinstance(decl, dict):
        return decl.get("type")
    return None


def _decl_default(decl):
    if isinstance(decl, (list, tuple)):
        return decl[1] if len(decl) > 1 else None
    if isinstance(decl, dict):
        return decl.get("default")
    return None


def new_container(props, type_name=None, with_tp=False):
    """空容器 / 无字段自定义对象的实例：只带 __id__（顶层可带 __tp__）。"""
    out = {"__id__": _ids.alloc_id(props)}
    if with_tp and type_name:
        out["__tp__"] = type_name
    return out


def new_instance(props, type_name, with_tp=False):
    """按 spec 声明建一个结构体实例（每个嵌套容器分配自己的 __id__）。

    props     : 玩家状态 dict（__id__ 计数器存在里面）
    type_name : spec_data.json 里的类型名（CharacterInfo / StoryTaskItem ...）
    with_tp   : 顶层对象 True；嵌套对象必须 False（见模块头 __tp__ 规则）

    返回的 dict 里业务字段都是声明里的默认值，调用方按需填。
    """
    if spec.custom(type_name) is None:
        raise ValueError(f"未知的自定义类型 {type_name!r}（spec_data.json 里没有）")
    out = {"__id__": _ids.alloc_id(props)}
    if with_tp:
        out["__tp__"] = type_name
    for fname, decl in spec.struct_fields(type_name).items():
        ftype = _decl_type(decl)
        if spec.is_struct(ftype):
            # 声明里出现结构体字段：递归建（当前 345 个类型里只到一层）
            out[fname] = new_instance(props, ftype)
        elif spec.is_container(ftype):
            out[fname] = new_container(props)
        else:
            out[fname] = _decl_default(decl)
    return out


def make_character(props, char_id):
    """建一个 CharacterInfo（CharacterPersistentDict 的值类型）。

    ★ 取代原来的「深拷贝另一个角色再改皮肤号」：
      深拷贝会把源角色的装备/皮肤/养成进度一起复制过去（9001 玩过之后
      再建 9002 就会继承），而且 193 个角色不可能逐个特例。
      这里按客户端声明的 18 个字段建干净实例，useSkinId 取
      CharData[char_id].DefaultSkin（9001 -> 900101 / 9002 -> 900201）。

    返回值是**嵌套**对象：不带 __tp__（父容器 CharacterPersistentDict 决定类型）。
    """
    from .. import gamedata
    info = new_instance(props, "CharacterInfo")
    cd = gamedata.char(char_id) or {}
    try:
        info["useSkinId"] = int(cd.get("DefaultSkin") or 0)
    except (TypeError, ValueError):
        info["useSkinId"] = 0
    return info
