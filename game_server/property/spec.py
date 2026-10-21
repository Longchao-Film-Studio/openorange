# -*- coding: utf-8 -*-
"""属性声明表（P0 地基 B1）。

数据来自 spec_data.json（由 tools/gen_property_spec.py 从客户端
generated/config.lua 机械生成，**不是手写**）。

## 为什么要有这一层

之前服务端对「属性是什么类型 / 键是整数还是字符串 / 是不是结构体」
全靠手写特例（avatar_init.json 生成器里 45 条 REQUIRED / SIMPLE_PROPS 注释），
加一个字段要改两处，而且容易和客户端声明脱节。

现在所有类型判断都从这里来，Tx（变更集）依赖它自动决定
OP_ADD / OP_CHANGE / OP_DEL 和键的转换方式。

## 术语

- **simple 类型**：int / double / string / boolean，直接给值
- **struct 类型**：Custom 里有 Properties 的（如 StreetInfo、ItemInfo），
  值是 {__id__, 字段...}
- **容器类型**：Custom 里有 __ValueType__ 的，值是 {key: value}
  - key_kind = "list"：类型名以 List 结尾（如 IntList、CharacterList），键是 "#N"
  - key_kind = "int" ：__IntTypeKey__ = true（如 ItemBag、StoryTaskBag），键是整数
  - key_kind = "str" ：__IntTypeKey__ = false，键是字符串
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "spec_data.json")
# 补充来源：这 45 个属性不在 AvatarProperties 里声明（只在 PropertyCallbacks 里），
# 它们的真实类型只能从 avatar_init.json 的 __tp__ 取。
_INIT_DICT = os.path.join(os.path.dirname(_HERE), "avatar_init.json")

SIMPLE_TYPES = ("int", "double", "string", "boolean")

_LOADED = None
_INIT_TYPES = None


def _load():
    global _LOADED
    if _LOADED is None:
        if not os.path.exists(_DATA):
            raise RuntimeError(
                "缺少 %s —— 先跑 python tools/gen_property_spec.py" % _DATA)
        with open(_DATA, "r", encoding="utf-8") as f:
            _LOADED = json.load(f)
    return _LOADED


def _init_types():
    """从 avatar_init.json 取「AvatarProperties 没声明」那些属性的真实类型。

    返回 {propName: {"type": <__tp__ 或简单类型>, "source": "initdict"}}。
    依据：item_bag / street_info / story_task_bag 这些只在 PropertyCallbacks 里出现，
    类型名是笼统的 customDict，具体是 ItemBag 还是 StreetInfoDict 只有 initDict 知道。
    """
    global _INIT_TYPES
    if _INIT_TYPES is not None:
        return _INIT_TYPES
    out = {}
    try:
        with open(_INIT_DICT, "r", encoding="utf-8") as f:
            props = json.load(f).get("__Properties__") or {}
    except Exception:
        _INIT_TYPES = out
        return out
    for name, val in props.items():
        if isinstance(val, dict) and val.get("__tp__"):
            out[name] = {"type": val["__tp__"], "source": "initdict"}
        elif isinstance(val, bool):
            out[name] = {"type": "boolean", "source": "initdict"}
        elif isinstance(val, int):
            out[name] = {"type": "int", "source": "initdict"}
        elif isinstance(val, float):
            out[name] = {"type": "double", "source": "initdict"}
        elif isinstance(val, str):
            out[name] = {"type": "string", "source": "initdict"}
    _INIT_TYPES = out
    return out


def reload_data():
    """测试用：丢掉缓存，重新读盘。"""
    global _LOADED, _INIT_TYPES
    _LOADED = None
    _INIT_TYPES = None
    return _load()


# ---------------------------------------------------------------- 类型判断
def is_simple(type_name):
    return type_name in SIMPLE_TYPES


def custom(type_name):
    """取自定义类型声明（struct / 容器）。没有返回 None。"""
    return (_load().get("custom_types") or {}).get(type_name)


def is_struct(type_name):
    """结构体：Custom 里有 Properties 且非空。

    注意：**可以和 is_container 同时为 True**（混合类型）。
    例：StreetInfoDict 既是 StreetInfo 的字典（__ValueType__），
    又有 3 个自身字段（all_prestige_exp / street_redpoint / surface_enter_time）。
    """
    c = custom(type_name)
    return bool(c and c.get("props"))


def is_container(type_name):
    """容器：Custom 里有 __ValueType__。

    注意：不要求没有 Properties —— StreetInfoDict 就是「字典 + 自身字段」的混合类型。
    """
    c = custom(type_name)
    return bool(c and c.get("value_type"))


def value_type(type_name):
    """容器的元素类型（如 ItemBag -> ItemInfo）。非容器返回 None。"""
    c = custom(type_name)
    return (c or {}).get("value_type")


def key_kind(type_name):
    """容器的键类型。

    返回：
      "list"  客户端类定义是 CustomList，键是 "#N"（msgpack 数组表示）
      "int"   是 CustomDict 且 __IntTypeKey__ = true，键是整数
      "str"   是 CustomDict 且 __IntTypeKey__ = false，键是字符串
      None    不是容器

    ★ 判定来源是客户端类定义（class.Class("X", CustomList|CustomDict)），
      不是「类型名以 List 结尾」的名字启发式。
      审计实测：按名字猜，342 个类型里错 7 个 ——
        ActivityCheckinRecords / GachaRecord / GachaRewards / Signs 是 list 但名字不带 List；
        ChampionshipScheduleInfoList / CharShownList / EndlessRankInfoRecordList 是 dict 但名字带 List。
    """
    if not is_container(type_name):
        return None
    c = custom(type_name) or {}
    kind = c.get("container_kind")
    if kind == "list":
        return "list"
    # 兜底：客户端类定义没找到时退回名字启发式
    if not kind and type_name.endswith("List"):
        return "list"
    return "int" if c.get("int_key") else "str"


def struct_fields(type_name):
    """结构体的字段表：{field: [type, default]}。非结构体返回 {}。"""
    c = custom(type_name)
    return dict((c or {}).get("props") or {})


# ---------------------------------------------------------------- 根属性
class PropSpec(object):
    """一个根属性的声明。

    source:
      "config"   —— 来自 AvatarProperties.Properties（权威声明，173 个）
      "initdict" —— 来自 avatar_init.json 的 __tp__（45 个，只在 PropertyCallbacks 里出现）
    """

    __slots__ = ("name", "type", "default", "scope", "persist", "source")

    def __init__(self, name, d):
        self.name = name
        self.type = d.get("type")
        self.default = d.get("default")
        self.scope = d.get("scope") or ""
        self.persist = d.get("persist") or ""
        self.source = d.get("source") or "config"

    @property
    def is_simple(self):
        return is_simple(self.type)

    @property
    def is_struct(self):
        return is_struct(self.type)

    @property
    def is_container(self):
        return is_container(self.type)

    @property
    def key_kind(self):
        return key_kind(self.type)

    @property
    def value_type(self):
        return value_type(self.type)

    @property
    def is_server_only(self):
        return self.scope == "ServerOnly"

    def __repr__(self):
        return "PropSpec(%s: %s%s)" % (
            self.name, self.type,
            " key=%s" % self.key_kind if self.key_kind else "")


def get(name):
    """取一个根属性的声明。没有返回 None。

    先查 AvatarProperties（权威声明），再回退到 avatar_init.json 的 __tp__。
    """
    d = (_load().get("avatar_properties") or {}).get(name)
    if d:
        return PropSpec(name, d)
    fallback = _init_types().get(name)
    if fallback:
        return PropSpec(name, fallback)
    return None


def all_props():
    """所有根属性名（173 个权威声明 + 45 个只在 initDict 里的 = 218）。"""
    names = set((_load().get("avatar_properties") or {}).keys())
    names |= set(_init_types().keys())
    return sorted(names)


def stats():
    d = _load()
    props = d.get("avatar_properties") or {}
    types = d.get("custom_types") or {}
    kinds = {"simple": 0, "struct": 0, "list": 0, "int": 0, "str": 0, "unknown": 0}
    for p in props.values():
        t = p.get("type")
        if is_simple(t):
            kinds["simple"] += 1
        elif is_struct(t):
            kinds["struct"] += 1
        elif is_container(t):
            kk = key_kind(t)
            kinds[kk if kk else "unknown"] += 1
        else:
            kinds["unknown"] += 1
    return {
        "root_props": len(all_props()),        # 173 声明 + 45 initDict 补齐 = 218
        "root_props_declared": len(props),     # AvatarProperties 里的 173
        "root_props_initdict_only": len(all_props()) - len(props),
        "custom_types": len(types),
        "structs": sum(1 for c in types.values() if c.get("props")),
        "containers": sum(1 for c in types.values()
                          if not c.get("props") and c.get("value_type")),
        "root_prop_kinds": kinds,
        "generated_from": d.get("generated_from"),
    }
