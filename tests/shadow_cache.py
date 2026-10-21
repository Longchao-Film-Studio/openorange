# -*- coding: utf-8 -*-
"""客户端属性缓存模拟器（P0 地基 C 项）。

## 为什么需要它

现有测试全部只断言「服务端 props 改对了」，**没有一条校验推出去的帧在客户端是否合法**。
所以 25 个套件 880 项全绿，客户端照样会崩 —— 2026-10-21 的审计就是这样抓出
4 类崩溃的（切街区 / 接任务 / 完成任务 / 开宝箱）。

本模块按客户端真实语义实现一个影子缓存，把服务端产生的每一条
propertySync 帧喂进去，记录「客户端会在哪一行崩」。

## 依据的客户端源码

| 文件 | 作用 |
|---|---|
| core/propertysync/puppetpropertycomponent.lua | 帧分发 + 5 个 optype 处理器 |
| core/propertysync/customtype.lua | _attachToCache / _detachFromCache（id repeated!） |
| core/propertysync/customdict.lua | roinit / rochange（assert(id ~= nil)） |
| core/propertysync/customlist.lua | CustomList 版本 |
| core/propertysync/propertydeclare.lua | createCustomObjRO / __ValueTypeDeclare__ |

## 关键的客户端断言（模拟器要复现的崩溃点）

1. initProperty:53      assert(value.__tp__ ~= nil)
2. _changeRootProperty:163  assert(customType ~= nil)
3. _changeRootProperty:166  oldVal:customTypeName()   <- oldVal 为 nil 时崩
4. _changeSubProperty:189   assert(customObj ~= nil)
5. _changeSubProperty:210   oldVal:customTypeName()   <- oldVal 为 nil 时崩
6. __OpAdd__:243        assert(customObj ~= nil)
7. __OpAdd__:246        assert(valueTypeDeclare ~= nil)
8. __OpDel__:288/291    assert(customObj ~= nil) / assert(valueTypeDeclare ~= nil)
9. __OpDel__:296        assert(obj ~= nil)
10. customdict.roinit:450   assert(id ~= nil)
11. customtype._attachToCache:236  重复 id -> logger:warn("id repeated!") + 覆盖缓存

## 用法

    from shadow_cache import ShadowCache
    sc = ShadowCache()
    sc.init_property(init_dict["__Properties__"])
    for frame in frames:
        sc.apply_frame(frame)
    assert sc.errors == [], sc.errors
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from game_server import ms as _ms
from game_server.property import spec as _spec


class CustomObj(object):
    """模拟客户端的 CustomDict / CustomList 实例。"""

    __slots__ = ("type_name", "id", "props", "parent", "name", "is_list",
                 "is_fixed")

    def __init__(self, type_name, oid, parent=None, name=None,
                 is_list=False, is_fixed=False):
        self.type_name = type_name
        self.id = oid
        self.props = {}
        self.parent = parent
        self.name = name
        self.is_list = is_list
        self.is_fixed = is_fixed

    def __repr__(self):
        return "<%s id=%s keys=%s>" % (
            self.type_name, self.id,
            sorted(str(k) for k in self.props)[:6])


class ShadowCache(object):
    """客户端 PuppetPropertyComponent 的影子实现。"""

    def __init__(self):
        self.props = {}      # __Properties__
        self.cache = {}      # __CustomPropertiesCache__: id -> CustomObj
        self.errors = []     # 客户端会崩的地方
        self.warnings = []   # id repeated / not found 这类日志
        self.frame_count = 0   # 已应用的属性同步帧数
        self.skipped = 0       # 非属性同步帧（实体消息/应答）跳过数

    # ------------------------------------------------------------ 诊断
    def _err(self, where, msg):
        self.errors.append("[%s] %s" % (where, msg))

    def _warn(self, where, msg):
        self.warnings.append("[%s] %s" % (where, msg))

    def ok(self):
        return not self.errors

    def report(self):
        lines = []
        if self.errors:
            lines.append("客户端会崩 %d 处：" % len(self.errors))
            lines.extend("  " + e for e in self.errors[:20])
        if self.warnings:
            lines.append("客户端警告 %d 条：" % len(self.warnings))
            lines.extend("  " + w for w in self.warnings[:20])
        return "\n".join(lines)

    # ------------------------------------------------------------ 缓存
    def _attach(self, obj, where="attach"):
        """customtype.lua:231 _attachToCache —— 递归挂到缓存，重复 id 告警。"""
        if not isinstance(obj, CustomObj):
            return
        if obj.id is None or obj.id < 0:
            return
        if obj.id in self.cache and self.cache[obj.id] is not obj:
            self._warn(where, "%s attach id %s repeated!（会覆盖缓存项）"
                       % (obj.type_name, obj.id))
        self.cache[obj.id] = obj
        for v in obj.props.values():
            self._attach(v, where)

    def _detach(self, obj, where="detach"):
        if not isinstance(obj, CustomObj):
            return
        if obj.id is None or obj.id < 0:
            return
        if obj.id not in self.cache:
            self._warn(where, "%s detach id %s not found!" % (obj.type_name, obj.id))
        self.cache.pop(obj.id, None)
        for v in obj.props.values():
            self._detach(v, where)

    # ------------------------------------------------------------ 构造
    def _create_ro(self, type_name, name, value, parent, fixed=True,
                   where="createRO"):
        """模拟 propertydeclare:createCustomObjRO。

        对应 customdict.lua:440 roinit / customlist.lua:375 roinit。
        """
        if not isinstance(value, dict):
            self._err(where, "%s 的值不是 dict（客户端会当简单值处理）" % name)
            return value
        # roinit:450  assert(id ~= nil)
        oid = value.get("__id__")
        if not isinstance(oid, int) or isinstance(oid, bool):
            self._err(where, "%s 没有 __id__ -> customdict.roinit:450 assert(id ~= nil)"
                      % name)
            return None
        is_list = _spec.key_kind(type_name) == "list"
        obj = CustomObj(type_name, oid, parent=parent, name=name,
                        is_list=is_list, is_fixed=fixed)
        fields = _spec.struct_fields(type_name)
        vtype = _spec.value_type(type_name)
        for k, v in value.items():
            if k in ("__id__",):
                continue
            if k == "__tp__":
                # 嵌套值不该带 __tp__。后果分两种：
                #   - 父类型是**结构体**（没有 __ValueTypeDeclare__）：
                #       roinit 里 declare = propDeclares['__tp__'] -> nil ->
                #       valueTypeDeclare 也是 nil -> 静默跳过（不崩，但脏）
                #   - 父类型是**容器**（有 __ValueType__）：
                #       declare = valueTypeDeclare -> createCustomObjRO('__tp__', 'Xxx')
                #       -> 对字符串执行 value.__id__ = nil -> 崩
                if vtype and (_spec.is_struct(vtype) or _spec.is_container(vtype)):
                    self._err(where,
                              "%s 的值里带 __tp__，且父是容器 -> 客户端会对字符串"
                              "取 __id__ 崩" % name)
                else:
                    self._warn(where,
                              "%s 的值里带 __tp__（结构体里会被静默丢弃，但应该剥掉）"
                              % name)
                continue
            fld = fields.get(k)
            declare = fld[0] if fld else vtype
            if declare is None:
                self._warn(where, "%s.%s 没有声明（客户端会静默跳过）" % (name, k))
                continue
            if _spec.is_struct(declare) or _spec.is_container(declare):
                child = self._create_ro(declare, k, v, obj, fixed, where)
                if child is not None:
                    obj.props[k] = child
            else:
                obj.props[k] = v
        return obj

    def init_property(self, init_dict):
        """模拟 puppetpropertycomponent.lua:38 initProperty(dict)。"""
        for name, value in init_dict.items():
            if isinstance(value, dict):
                # initProperty:52-53  assert(value.__tp__ ~= nil)
                custom_type = value.get("__tp__")
                if custom_type is None:
                    self._err("initProperty",
                              "%s 是表但没有 __tp__ -> assert(customType ~= nil)" % name)
                    continue
                v = {k: vv for k, vv in value.items() if k != "__tp__"}
                obj = self._create_ro(custom_type, name, v, None, True,
                                      "initProperty")
                if obj is None:
                    continue
                self._attach(obj, "initProperty")
                self.props[name] = obj
            else:
                self.props[name] = value
        return self

    # ------------------------------------------------------------ 帧应用
    def apply_frame(self, frame_bytes):
        """解析一条 propertySync 帧并应用到影子缓存。返回 True 表示成功。"""
        if not isinstance(frame_bytes, (bytes, bytearray)):
            return False
        f = bytes(frame_bytes)
        if len(f) < 6:
            return False
        # ★ 只处理属性同步帧（type=13）。实体消息 / 应答帧跳过。
        ftype = int.from_bytes(f[4:6], "little")
        if ftype != _ms.PROPERTY_SYNC_TYPE:
            self.skipped += 1
            return False
        # 帧格式：[u32 len][u16 type] + body
        fields = _ms.pb_parse(f[6:])
        pid = fields.get(2, [None])[0]
        optype = fields.get(3, [None])[0]
        name_raw = fields.get(4, [b""])[0]
        val_raw = fields.get(5, [None])[0]
        if pid is None or optype is None:
            return False
        name = name_raw.decode("utf-8", "replace") if isinstance(
            name_raw, (bytes, bytearray)) else name_raw
        value = None
        if val_raw is not None:
            try:
                value, _ = _ms.mp_decode(val_raw)
            except Exception as e:
                self._err("decode", "参数解不出来: %s" % e)
                return False
        self.frame_count += 1
        if optype == _ms.OP_DEL:
            self._op_del(pid, name)
        elif optype == _ms.OP_REMOVE:
            self._op_remove(pid, name)
        elif optype == _ms.OP_CHANGE:
            self._op_change(pid, name, value)
        elif optype == _ms.OP_ADD:
            self._op_add(pid, name, value)
        elif optype == _ms.OP_INSERT:
            self._op_insert(pid, name, value)
        else:
            self._err("dispatch", "未知 optype %s" % optype)
            return False
        return True

    # ------------------------------------------------------------ 5 个 optype
    def _op_change(self, pid, name, value):
        """puppetpropertycomponent.lua:233 __OpChange__。"""
        if pid <= 0:
            return self._change_root(name, value)
        return self._change_sub(pid, name, value)

    def _change_root(self, name, value):
        """puppetpropertycomponent.lua:158 _changeRootProperty。"""
        if isinstance(value, dict):
            # :163  assert(customType ~= nil)
            custom_type = value.get("__tp__")
            if custom_type is None:
                self._err("_changeRootProperty",
                          "根属性 %s 是表但没有 __tp__ -> :163 assert(customType ~= nil)"
                          % name)
                return
            v = {k: vv for k, vv in value.items() if k != "__tp__"}
            old = self.props.get(name)
            # :166  oldVal:customTypeName() —— oldVal 为 nil 就崩
            if old is None:
                self._err("_changeRootProperty",
                          "根属性 %s 在客户端不存在 -> :166 oldVal:customTypeName() 索引 nil"
                          % name)
                return
            if isinstance(old, CustomObj) and old.id == v.get("__id__") and old.id > 0:
                # 原地 rochange
                self._rochange(old, v, "_changeRootProperty")
            else:
                if isinstance(old, CustomObj):
                    self._detach(old, "_changeRootProperty")
                new_obj = self._create_ro(custom_type, name, v, None, True,
                                          "_changeRootProperty")
                if new_obj is None:
                    return
                self._attach(new_obj, "_changeRootProperty")
                self.props[name] = new_obj
        else:
            self.props[name] = value

    def _change_sub(self, pid, name, value):
        """puppetpropertycomponent.lua:186 _changeSubProperty。"""
        obj = self.cache.get(pid)
        # :189  assert(customObj ~= nil)
        if obj is None:
            self._err("_changeSubProperty",
                      "propertyId=%s 不在客户端缓存 -> :189 assert(customObj ~= nil)"
                      % pid)
            return
        # :190  assert(v ~= nil)
        if value is None:
            self._err("_changeSubProperty",
                      "pid=%s name=%s 的值为 nil -> :190 assert(v ~= nil)" % (pid, name))
            return
        fields = _spec.struct_fields(obj.type_name)
        vtype = _spec.value_type(obj.type_name)
        if obj.is_list:
            k = _to_num(name)
            declare = vtype
        else:
            fld = fields.get(name)
            if fld is None:
                k = _to_num(name) if _int_key(obj.type_name) else name
                declare = vtype
            else:
                k = name
                declare = fld[0]
        if isinstance(value, dict):
            old = obj.props.get(k)
            # :210  oldVal:customTypeName() —— oldVal 为 nil 就崩
            if old is None:
                self._err("_changeSubProperty",
                          "%s[%r] 在客户端不存在 -> :210 oldVal:customTypeName() 索引 nil"
                          "（新键应该用 OP_ADD）" % (obj.type_name, k))
                return
            if isinstance(old, CustomObj) and old.id == value.get("__id__") and old.id > 0:
                self._rochange(old, value, "_changeSubProperty")
            else:
                if isinstance(old, CustomObj):
                    self._detach(old, "_changeSubProperty")
                if declare is None:
                    self._err("_changeSubProperty",
                              "%s[%r] 没有类型声明 -> declare:createCustomObjRO 崩"
                              % (obj.type_name, k))
                    return
                new_obj = self._create_ro(declare, k, value, obj, False,
                                          "_changeSubProperty")
                if new_obj is None:
                    return
                self._attach(new_obj, "_changeSubProperty")
                obj.props[k] = new_obj
        else:
            obj.props[k] = value

    def _op_add(self, pid, name, value):
        """puppetpropertycomponent.lua:241 __OpAdd__。"""
        obj = self.cache.get(pid)
        if obj is None:      # :243
            self._err("__OpAdd__",
                      "propertyId=%s 不在客户端缓存 -> :243 assert(customObj ~= nil)"
                      % pid)
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :246
            self._err("__OpAdd__",
                      "%s 没有 __ValueTypeDeclare__ -> :246 assert(valueTypeDeclare ~= nil)"
                      % obj.type_name)
            return
        k = _to_num(name) if _int_key(obj.type_name) else name
        if isinstance(value, dict):
            child = self._create_ro(vtype, k, value, obj, False, "__OpAdd__")
            if child is None:
                return
            self._attach(child, "__OpAdd__")
            obj.props[k] = child
        else:
            obj.props[k] = value

    def _op_del(self, pid, name):
        """puppetpropertycomponent.lua:286 __OpDel__。"""
        obj = self.cache.get(pid)
        if obj is None:      # :288
            self._err("__OpDel__",
                      "propertyId=%s 不在客户端缓存 -> :288 assert(customObj ~= nil)" % pid)
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :291
            self._err("__OpDel__",
                      "%s 没有 __ValueTypeDeclare__ -> :291 assert(valueTypeDeclare ~= nil)"
                      "（结构体字段不能删）" % obj.type_name)
            return
        k = _to_num(name) if _int_key(obj.type_name) else name
        old = obj.props.get(k)
        if old is None:      # :296
            self._err("__OpDel__",
                      "%s[%r] 不存在 -> :296 assert(obj ~= nil)" % (obj.type_name, k))
            return
        if isinstance(old, CustomObj):
            self._detach(old, "__OpDel__")
        del obj.props[k]

    def _op_insert(self, pid, name, value):
        obj = self.cache.get(pid)
        if obj is None:
            self._err("__OpInsert__", "propertyId=%s 不在缓存" % pid)
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:
            self._err("__OpInsert__", "%s 没有 __ValueTypeDeclare__" % obj.type_name)
            return
        k = _to_num(name)
        if isinstance(value, dict):
            child = self._create_ro(vtype, k, value, obj, False, "__OpInsert__")
            if child is None:
                return
            self._attach(child, "__OpInsert__")
            obj.props[k] = child
        else:
            obj.props[k] = value

    def _op_remove(self, pid, name):
        obj = self.cache.get(pid)
        if obj is None:
            self._err("__OpRemove__", "propertyId=%s 不在缓存" % pid)
            return
        k = _to_num(name)
        obj.props.pop(k, None)

    def _rochange(self, obj, value, where):
        """customdict.lua:481 rochange —— 只更新已有键。"""
        oid = value.get("__id__")
        if oid is None:
            self._err(where, "%s rochange 缺 __id__ -> :488 assert(id ~= nil)"
                      % obj.type_name)
            return
        if oid != obj.id:
            self._err(where, "%s rochange id 不匹配（%s vs %s）-> :490 日志并返回"
                      % (obj.type_name, obj.id, oid))
            return
        fields = _spec.struct_fields(obj.type_name)
        vtype = _spec.value_type(obj.type_name)
        for k, v in value.items():
            if k == "__id__":
                continue
            if k == "__tp__":
                if vtype and (_spec.is_struct(vtype) or _spec.is_container(vtype)):
                    self._err(where,
                              "%s rochange 的 value 里带 __tp__，父是容器 -> 崩"
                              % obj.type_name)
                else:
                    self._warn(where,
                              "%s rochange 的 value 里带 __tp__（静默丢弃）"
                              % obj.type_name)
                continue
            fld = fields.get(k)
            declare = fld[0] if fld else vtype
            ov = obj.props.get(k)
            if ov is not None and isinstance(ov, CustomObj):
                if isinstance(v, dict):
                    self._rochange(ov, v, where)
                continue
            if isinstance(v, dict):
                if declare is None:
                    continue
                child = self._create_ro(declare, k, v, obj, False, where)
                if child is not None:
                    self._attach(child, where)
                    obj.props[k] = child
            else:
                obj.props[k] = v




def frames_safe(init_dict, frames, tag=""):
    """便捷入口：建影子缓存 -> 喂帧 -> 返回 (sc, errors)。

    给现有测试用的：把 handler 产出的帧直接喂进来，断言客户端不崩。

    用法：
        from shadow_cache import frames_safe
        sc, errs = frames_safe(msmod._live_init_dict(uid), frames)
        assert not errs, errs
    """
    props = (init_dict or {}).get("__Properties__") or init_dict or {}
    sc = ShadowCache()
    sc.init_property(props)
    for f in frames:
        sc.apply_frame(f)
    if tag and sc.errors:
        print("    [%s] 客户端会崩 %d 处：" % (tag, len(sc.errors)))
        for e in sc.errors[:5]:
            print("      " + e[:110])
    return sc, list(sc.errors)

def _to_num(k):
    """客户端 intTypeKey 的 tonumber(k)。"""
    if isinstance(k, int):
        return k
    try:
        return int(k)
    except (TypeError, ValueError):
        return k


def _int_key(type_name):
    """该容器的键是不是整数。"""
    return _spec.key_kind(type_name) == "int"
