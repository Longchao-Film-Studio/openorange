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


class CustomObj:
    """模拟客户端的 CustomDict / CustomList 实例。"""

    __slots__ = (
        "id",
        "is_fixed",
        "is_list",
        "name",
        "parent",
        "props",
        "type_name",
    )

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
        return f"<{self.type_name} id={self.id} keys={sorted(str(k) for k in self.props)[:6]}>"


class ShadowCache:
    """客户端 PuppetPropertyComponent 的影子实现。"""

    def __init__(self):
        self.props = {}      # __Properties__
        self.cache = {}      # __CustomPropertiesCache__: id -> CustomObj
        self.errors = []     # 客户端会崩的地方
        self.warnings = []   # id repeated / not found 这类日志
        # 已应用的属性同步帧数
        self.frame_count = 0
        # 帧里出现过的非空 ownerid（空串表示收帧者自己，不记）
        self.ownerids = set()
        # 因为 owner 未知而被丢掉的帧数（客户端 getRealOwner 拿不到也是整帧丢弃）
        self.owner_frames_dropped = 0   # 已应用的属性同步帧数
        self.skipped = 0       # 非属性同步帧（实体消息/应答）跳过数

    # ------------------------------------------------------------ 诊断
    def _err(self, where, msg):
        self.errors.append(f"[{where}] {msg}")

    def _warn(self, where, msg):
        self.warnings.append(f"[{where}] {msg}")

    def ok(self):
        return not self.errors

    def report(self):
        lines = []
        if self.errors:
            lines.append(f'客户端会崩 {len(self.errors)} 处：')
            lines.extend("  " + e for e in self.errors[:20])
        if self.warnings:
            lines.append(f'客户端警告 {len(self.warnings)} 条：')
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
            self._warn(where, f"{obj.type_name} attach id {obj.id} repeated!（会覆盖缓存项）")
        self.cache[obj.id] = obj
        for v in obj.props.values():
            self._attach(v, where)

    def _detach(self, obj, where="detach"):
        if not isinstance(obj, CustomObj):
            return
        if obj.id is None or obj.id < 0:
            return
        if obj.id not in self.cache:
            self._warn(where, f"{obj.type_name} detach id {obj.id} not found!")
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
            self._err(where, f"{name} 的值不是 dict（客户端会当简单值处理）")
            return value
        # roinit:450  assert(id ~= nil)
        oid = value.get("__id__")
        if not isinstance(oid, int) or isinstance(oid, bool):
            self._err(where, f"{name} 没有 __id__ -> customdict.roinit:450 assert(id ~= nil)")
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
                              f"{name} 的值里带 __tp__，且父是容器 -> 客户端会对字符串"
                              "取 __id__ 崩")
                else:
                    self._warn(where,
                              f"{name} 的值里带 __tp__（结构体里会被静默丢弃，但应该剥掉）")
                continue
            fld = fields.get(k)
            declare = fld[0] if fld else vtype
            if declare is None:
                self._warn(where, f"{name}.{k} 没有声明（客户端会静默跳过）")
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
                              f"{name} 是表但没有 __tp__ -> assert(customType ~= nil)")
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
        # ownerid：空串 = 收帧者自己；非空 = 别人的对象（客户端会 getRealOwner）
        owner_raw = fields.get(1, [b""])[0]
        ownerid = (owner_raw.decode("utf-8", "replace")
                   if isinstance(owner_raw, (bytes, bytearray)) else owner_raw)
        if ownerid:
            self.ownerids.add(ownerid)
            # ★ 客户端 gateclientrpchandler.lua:269-288：ownerid 非空时先
            #   getRealOwner(ownerid)，拿不到就 warn + return —— **整帧丢弃**，
            #   不会应用到收帧者自己的缓存上。
            #   影子缓存只建模「自己」这一份缓存（单机私服服务端从不推别人的属性，
            #   见 game_server/property/sync.py 的 SYNC_SELF），所以非空 ownerid
            #   一律按「未知 owner」丢弃，并记数。
            self.owner_frames_dropped += 1
            return False
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
                self._err("decode", f"参数解不出来: {e}")
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
            self._err("dispatch", f"未知 optype {optype}")
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
                          f"根属性 {name} 是表但没有 __tp__ -> :163 assert(customType ~= nil)")
                return
            v = {k: vv for k, vv in value.items() if k != "__tp__"}
            old = self.props.get(name)
            # :166  oldVal:customTypeName() —— oldVal 为 nil 就崩
            if old is None:
                self._err("_changeRootProperty",
                          f"根属性 {name} 在客户端不存在 -> :166 oldVal:customTypeName() 索引 nil")
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
                      f"propertyId={pid} 不在客户端缓存 -> :189 assert(customObj ~= nil)")
            return
        # :190  assert(v ~= nil)
        if value is None:
            self._err("_changeSubProperty",
                      f"pid={pid} name={name} 的值为 nil -> :190 assert(v ~= nil)")
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
                          f"{obj.type_name}[{k!r}] 在客户端不存在 -> :210 oldVal:customTypeName() 索引 nil"
                          "（新键应该用 OP_ADD）")
                return
            if isinstance(old, CustomObj) and old.id == value.get("__id__") and old.id > 0:
                self._rochange(old, value, "_changeSubProperty")
            else:
                if isinstance(old, CustomObj):
                    self._detach(old, "_changeSubProperty")
                if declare is None:
                    self._err("_changeSubProperty",
                              f"{obj.type_name}[{k!r}] 没有类型声明 -> declare:createCustomObjRO 崩")
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
                      f"propertyId={pid} 不在客户端缓存 -> :243 assert(customObj ~= nil)")
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :246
            self._err("__OpAdd__",
                      f"{obj.type_name} 没有 __ValueTypeDeclare__ -> :246 assert(valueTypeDeclare ~= nil)")
            return
        k = _to_num(name) if _int_key(obj.type_name) else name
        if isinstance(k, _NilKey):
            self._err("__OpAdd__",
                      f"{obj.type_name} 的键 {name!r} 转不成数字 -> :248 tonumber 返回 nil -> "
                      "_properties[nil] 崩")
            return
        if isinstance(value, dict):
            if not _is_custom(vtype):
                self._err("__OpAdd__",
                          f"{obj.type_name} 的元素类型 {vtype} 不是自定义类型，但值是个表 -> "
                          ":252 valueTypeDeclare.customClass 为 nil，调 nil 崩")
                return
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
                      f"propertyId={pid} 不在客户端缓存 -> :288 assert(customObj ~= nil)")
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :291
            self._err("__OpDel__",
                      f"{obj.type_name} 没有 __ValueTypeDeclare__ -> :291 assert(valueTypeDeclare ~= nil)"
                      "（结构体字段不能删）")
            return
        k = _to_num(name) if _int_key(obj.type_name) else name
        if isinstance(k, _NilKey):
            self._err("__OpDel__",
                      f"{obj.type_name} 的键 {name!r} 转不成数字 -> :293 tonumber 返回 nil")
            return
        old = obj.props.get(k)
        if old is None:      # :296
            self._err("__OpDel__",
                      f"{obj.type_name}[{k!r}] 不存在 -> :296 assert(obj ~= nil)")
            return
        if isinstance(old, CustomObj):
            self._detach(old, "__OpDel__")
        del obj.props[k]

    def _op_insert(self, pid, name, value):
        """puppetpropertycomponent.lua:329 __OpInsert__ —— 只对 list 有意义。"""
        obj = self.cache.get(pid)
        if obj is None:      # :331
            self._err("__OpInsert__",
                      f"propertyId={pid} 不在缓存 -> :331 assert(customObj ~= nil)")
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :336
            self._err("__OpInsert__",
                      f"{obj.type_name} 没有 __ValueTypeDeclare__ -> :336 assert 崩")
            return
        k = _to_num(name)   # :333 无条件 tonumber
        if isinstance(k, _NilKey) or k < 1:
            self._err("__OpInsert__",
                      f"位置 {name!r} 非法 -> :333 tonumber 失败或 <1")
            return
        n = len(obj.props)
        if k > n + 1:        # table.insert 越界
            self._err("__OpInsert__",
                      f'位置 {k} 越界（当前 {n} 个元素）-> :340 table.insert 崩')
            return
        if isinstance(value, dict):
            child = self._create_ro(vtype, k, value, obj, False, "__OpInsert__")
            if child is None:
                return
            self._attach(child, "__OpInsert__")
        else:
            child = value
        # 模拟 table.insert 的位移
        new_props = {}
        for kk, vv in obj.props.items():
            new_props[kk if kk < k else kk + 1] = vv
        new_props[k] = child
        obj.props = new_props

    def _op_remove(self, pid, name):
        """puppetpropertycomponent.lua:377 __OpRemove__ —— 只对 list 有意义。"""
        obj = self.cache.get(pid)
        if obj is None:      # :379
            self._err("__OpRemove__",
                      f"propertyId={pid} 不在缓存 -> :379 assert(customObj ~= nil)")
            return
        vtype = _spec.value_type(obj.type_name)
        if vtype is None:    # :383
            self._err("__OpRemove__",
                      f"{obj.type_name} 没有 __ValueTypeDeclare__ -> :383 assert 崩")
            return
        k = _to_num(name)
        if isinstance(k, _NilKey):
            self._err("__OpRemove__", f"索引 {name!r} 转不成数字")
            return
        obj2 = obj.props.get(k)
        if obj2 is None:     # :385
            self._err("__OpRemove__",
                      f"{obj.type_name}[{k}] 不存在 -> :385 assert(obj ~= nil)")
            return
        if isinstance(obj2, CustomObj):
            self._detach(obj2, "__OpRemove__")
        # 模拟 table.remove 的位移
        new_props = {}
        for kk, vv in obj.props.items():
            if kk == k:
                continue
            new_props[kk if kk < k else kk - 1] = vv
        obj.props = new_props

    def _rochange(self, obj, value, where):
        """customdict.lua:481 rochange —— 严格按客户端算法。

        客户端算法（:496-541）：
          for k, v in pairs(value):
            ov = self._properties[k]
            if ov ~= nil:
              if type(ov) == table:
                if ov._id == v.__id__ and ov._id > 0: ov:rochange(v)
                else: detach; declare = propDeclares[k] or valueTypeDeclare;
                      if declare:isCustomType(): create+attach
              else: self._properties[k] = v      # 旧值是原生 -> 直接覆盖
            else:                                 # ★ 新键
              assert(propDeclares[k] == nil)      # 结构体字段当新键 -> 崩
              if valueTypeDeclare:isCustomType()  # 结构体 vtype 为 nil -> 崩
                then create+attach
          然后删除 value 里没有的键（同样 assert propDeclares[k] == nil）

        ★ 2026-10-21 审计 #9：__tp__ 是**新键**（结构体没有这个字段），
          走到 else 分支 -> valueTypeDeclare 为 nil -> :519 索引 nil 崩。
          之前我只 warn，漏检。
        """
        oid = value.get("__id__")
        if oid is None:
            self._err(where, f"{obj.type_name} rochange 缺 __id__ -> :488 assert(id ~= nil)")
            return
        if oid != obj.id:
            self._warn(where, f"{obj.type_name} rochange id 不匹配（{obj.id} vs {oid}）-> :490 日志并返回")
            return
        fields = _spec.struct_fields(obj.type_name)
        vtype = _spec.value_type(obj.type_name)
        for k, v in value.items():
            if k == "__id__":
                continue
            ov = obj.props.get(k)
            if ov is not None:
                if isinstance(ov, CustomObj):
                    if isinstance(v, dict) and ov.id == v.get("__id__") and ov.id > 0:
                        self._rochange(ov, v, where)
                        continue
                    fld = fields.get(k)
                    declare = fld[0] if fld else vtype
                    if declare is None:
                        self._err(where,
                                  f"{obj.type_name} rochange {k!r} 需要重建但没有 declare -> :508 崩")
                        continue
                    self._detach(ov, where)
                    if _is_custom(declare):
                        child = self._create_ro(declare, k, v, obj, False, where)
                        if child is not None:
                            self._attach(child, where)
                            obj.props[k] = child
                    else:
                        obj.props[k] = v
                else:
                    obj.props[k] = v
                continue
            # ★ 新键
            if k in fields:
                self._err(where,
                          f"{obj.type_name} rochange 把结构体字段 {k!r} 当新键 -> :518 "
                          "assert(propDeclares[k] == nil) 崩")
                continue
            if vtype is None:
                self._err(where,
                          f"{obj.type_name} rochange 遇到新键 {k!r}，且是结构体（valueTypeDeclare 为 nil）"
                          " -> :519 索引 nil 崩")
                continue
            if _is_custom(vtype):
                child = self._create_ro(vtype, k, v, obj, False, where)
                if child is not None:
                    self._attach(child, where)
                    obj.props[k] = child
            else:
                obj.props[k] = v
        # 删除 value 里没有的键（:527-541）
        for k in list(obj.props):
            if k in value:
                continue
            ov = obj.props[k]
            if isinstance(ov, CustomObj):
                self._detach(ov, where)
            if k in fields:
                self._err(where,
                          f"{obj.type_name} rochange 删除结构体字段 {k!r} -> :538 assert 崩")
                continue
            del obj.props[k]


def _is_custom(type_name):
    """该类型是不是自定义类型（结构体或容器）。"""
    return bool(type_name) and (_spec.is_struct(type_name)
                                or _spec.is_container(type_name))




def frames_safe(pre_init_dict, frames, tag=""):
    """便捷入口：建影子缓存 -> 喂帧 -> 返回 (sc, errors)。

    ★ pre_init_dict 必须是 **RPC 调用之前** 的 initDict！
      审计 P0-3 实测：如果用 RPC 之后的 initDict，客户端缓存里已经有
      那条新数据了，于是「新键用 OP_CHANGE」这种回归**抓不到**。
      正确用法：

        pre = msmod._live_init_dict(uid)      # 先快照
        frames = erpc.handle(...)             # 再跑 RPC
        sc, errs = frames_safe(pre, frames)   # 用 pre 建缓存
        assert not errs, errs
    """
    props = ((pre_init_dict or {}).get("__Properties__")
             or pre_init_dict or {})
    sc = ShadowCache()
    sc.init_property(props)
    for f in frames:
        sc.apply_frame(f)
    if tag and sc.errors:
        print(f'    [{tag}] 客户端会崩 {len(sc.errors)} 处：')
        for e in sc.errors[:5]:
            print("      " + e[:110])
    return sc, list(sc.errors)


class _NilKey:
    """客户端 tonumber(k) 失败返回 nil —— 拿 nil 当 table 键会崩。"""
    def __repr__(self):
        return "<nil>"


def _to_num(k):
    """客户端 intTypeKey 的 tonumber(k)。失败返回 _NilKey（代表 nil）。"""
    if isinstance(k, int) and not isinstance(k, bool):
        return k
    try:
        return int(k)
    except (TypeError, ValueError):
        return _NilKey()


def _int_key(type_name):
    """该容器的键是不是整数（客户端 __IntTypeKey__）。

    ★ 审计 P1：**list 类型的 __IntTypeKey__ 也是 true**，
      客户端对 list 也做 tonumber(k)。之前用 key_kind=="int" 判断，
      导致 IntList / CharacterList 的键没转 -> 误报。
    """
    return _spec.int_type_key(type_name)
