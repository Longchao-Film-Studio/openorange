# -*- coding: utf-8 -*-
"""变更集 Tx（P0 地基 B2）。

## 为什么要有这一层

审计发现：项目里有 36 处手拼 build_property_sync，optype 和 propertyId
全靠调用方人肉选。结果是：

    optype 分布：OP_CHANGE 27 / OP_ADD 7 / OP_DEL 2 / OP_INSERT 0 / OP_REMOVE 0

- 同一个坑（新键必须 OP_ADD）修了 2 处漏了 5 处
- OP_INSERT / OP_REMOVE 定义了从未使用 -> 列表类属性只能整表替换
- propertyId 手填 0 还是父对象 __id__，全靠人肉记忆

Tx 把「改哪里、改成什么」和「怎么编码成帧」彻底分开：
handler 只描述**意图**（tx.set(path, value)），Tx 负责

- 查 spec 校验类型和键类型
- 自动决定 OP_ADD / OP_CHANGE / OP_DEL
- 自动算 propertyId（根=0，子项=父对象 __id__）
- 自动给新对象分配 __id__（走单一分配器）
- 自动剔除 _oo_* 私有字段（由 sync 层做）
- 自动排序（先父后子，由 sync 层做）

## 路径化 API

一切都是路径。path[0] 是根属性名，后面依次是容器键 / 结构体字段名。

    tx.set(("street_id",), 1011)                            # 根属性
    tx.set(("street_info", 1011), street_info_obj)          # 容器里新建/覆盖
    tx.set(("street_info", 1011, "inter_actor_info"), ia)   # 结构体字段
    tx.delete(("story_task_bag", 1000))                     # 容器里删除

## 用法

    from .property.tx import Tx
    tx = Tx(st.props)
    tx.set(("street_id",), 1011)
    frames = tx.frames()      # -> [bytes]，由 sync.py 生成
    st.save()
"""
from . import ids as _ids
from . import spec as _spec

# optype（和客户端 puppetpropertycomponent.lua 一致）
OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3
OP_INSERT = 4
OP_REMOVE = 5


class TxError(Exception):
    pass


class Op(object):
    """一条待发送的变更。"""

    __slots__ = ("property_id", "optype", "name", "value", "path")

    def __init__(self, property_id, optype, name, value, path):
        self.property_id = property_id
        self.optype = optype
        self.name = name
        self.value = value
        self.path = path

    def __repr__(self):
        return "Op(pid=%r op=%d name=%r path=%r)" % (
            self.property_id, self.optype, self.name, self.path)


class Tx(object):
    """一次 RPC 内累积的变更集。

    所有改动**同时**写进 props（服务端状态）和 ops（待发送帧），
    保证两边一致。
    """

    __slots__ = ("props", "ops", "_touched_roots")

    def __init__(self, props):
        if not isinstance(props, dict):
            raise TxError("Tx 需要 props dict，收到 %r" % (type(props),))
        self.props = props
        self.ops = []
        self._touched_roots = []

    # ------------------------------------------------------------ 内部
    def _walk(self, path):
        """沿 path 走到父对象，返回 (parent, last_key, parent_pid, parent_type)。

        parent_pid  发送这条变更时该填的 propertyId（根=0，子项=父对象 __id__）
        parent_type 父对象的类型名（用来判断「容器键」还是「结构体字段」）

        类型推导：path[0] 的类型查 spec，之后每一段用上一层的 value_type。
        """
        if not path:
            raise TxError("path 不能为空")
        root = path[0]
        sp = _spec.get(root)
        if len(path) == 1:
            return self.props, root, 0, (sp.type if sp else None)
        if root not in self.props:
            raise TxError("根属性 %r 不存在，先 set 它" % (root,))
        cur = self.props[root]
        cur_pid = cur.get("__id__") if isinstance(cur, dict) else None
        cur_type = sp.type if sp else None
        if not isinstance(cur_pid, int):
            raise TxError("根属性 %r 没有 __id__，不能改它的子项" % (root,))
        for i, key in enumerate(path[1:-1], start=1):
            if not isinstance(cur, dict):
                raise TxError("路径 %r 第 %d 段 %r 的父不是 dict" % (path, i, key))
            if key not in cur:
                raise TxError("路径 %r 第 %d 段 %r 不存在" % (path, i, key))
            cur = cur[key]
            nid = cur.get("__id__") if isinstance(cur, dict) else None
            if not isinstance(nid, int):
                raise TxError("路径 %r 第 %d 段 %r 没有 __id__" % (path, i, key))
            cur_pid = nid
            cur_type = _spec.value_type(cur_type) or cur_type
        return cur, path[-1], cur_pid, cur_type

    def _ensure_id(self, value):
        """值是新对象且没有 __id__ 时，分配一个。"""
        if isinstance(value, dict) and "__id__" not in value:
            value["__id__"] = _ids.alloc_id(self.props)
        return value

    def _root_spec(self, root):
        return _spec.get(root)

    # ------------------------------------------------------------ 写 API
    def set(self, path, value):
        """设置某个路径的值。自动决定 OP_ADD / OP_CHANGE。

        path 可以是字符串（单段）或 tuple / list。
        """
        if isinstance(path, str):
            path = (path,)
        else:
            path = tuple(path)
        parent, key, pid, parent_type = self._walk(path)

        if len(path) == 1:
            # 根属性：客户端一定已经有这个根（initDict 下发过），所以是 CHANGE
            sp = self._root_spec(key)
            if sp is not None and sp.is_server_only:
                raise TxError("根属性 %r 是 ServerOnly，不该推给客户端" % (key,))
            self._ensure_id(value)
            parent[key] = value
            self._touch(key)
            self.ops.append(Op(0, OP_CHANGE, str(key), value, path))
            return value

        if not isinstance(parent, dict):
            raise TxError("路径 %r 的父不是 dict" % (path,))
        # ★ 区分「结构体字段」和「容器键」：
        #   - 结构体字段：客户端从 initDict 就带着全部字段 -> 永远是 CHANGE
        #   - 容器键：可能是新键 -> OP_ADD
        #
        # 注意不能只看「父类型是不是结构体」—— StreetInfoDict 是混合类型
        # （既是 StreetInfo 的字典，又有 all_prestige_exp 等 3 个自身字段）。
        # 所以要看「这个 key 是不是父类型声明的字段」。
        fields = _spec.struct_fields(parent_type) if parent_type else {}
        is_struct_field = key in fields
        is_new = key not in parent          # ★ 必须在赋值之前判断
        if isinstance(value, dict):
            self._ensure_id(value)
        parent[key] = value
        optype = OP_CHANGE if (is_struct_field or not is_new) else OP_ADD
        self.ops.append(Op(pid, optype, _key_str(key), value, path))
        return value

    def delete(self, path):
        """删除某个路径（容器里的一个键）。"""
        if isinstance(path, str):
            raise TxError("不能删除根属性")
        path = tuple(path)
        parent, key, pid, _ptype = self._walk(path)
        if not isinstance(parent, dict):
            raise TxError("路径 %r 的父不是 dict" % (path,))
        if key not in parent:
            raise TxError("路径 %r 的键不存在，不能删除" % (path,))
        del parent[key]
        self.ops.append(Op(pid, OP_DEL, _key_str(key), None, path))
        return True

    del_ = delete

    def _touch(self, root):
        if root not in self._touched_roots:
            self._touched_roots.append(root)

    # ------------------------------------------------------------ 读 API
    def get(self, path, default=None):
        """读某个路径的当前值（读的是 props，不是待发送队列）。"""
        if isinstance(path, str):
            path = (path,)
        else:
            path = tuple(path)
        cur = self.props
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    # ------------------------------------------------------------ 提交
    def frames(self, strict=False):
        """把 ops 编成同步帧。延迟导入避免循环依赖。

        strict=True 时校验失败直接抛异常（测试用）；生产路径用 False。
        """
        from . import sync as _sync
        return _sync.ops_to_frames(self.ops, strict=strict)

    def commit(self):
        """标记本次变更已写进 props（save 由调用方决定）。"""
        return len(self.ops)

    def __len__(self):
        return len(self.ops)

    def __repr__(self):
        return "Tx(%d ops)" % len(self.ops)


def _key_str(key):
    """容器键转成帧里的 name 字段。

    规则（和客户端 CustomDict 一致）：
      - 字符串键：原样
      - 整数键：转成字符串（msgpack 里 name 是 string 字段）
    """
    return key if isinstance(key, str) else str(key)
