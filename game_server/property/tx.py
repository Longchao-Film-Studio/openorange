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
from shared.log_tee import get_logger

from ..write_guard import write_guard
from . import ids as _ids
from . import spec as _spec
from . import validate as _validate

OP_REMOVE = 5

_log = get_logger(__name__)

# optype（和客户端 puppetpropertycomponent.lua 一致）
OP_CHANGE = 1
OP_ADD = 2
OP_DEL = 3
OP_INSERT = 4
OP_REMOVE = 5


class TxError(Exception):
    pass


class Op:
    """一条待发送的变更。"""

    __slots__ = ("name", "optype", "path", "property_id", "value")

    def __init__(self, property_id, optype, name, value, path):
        self.property_id = property_id
        self.optype = optype
        self.name = name
        self.value = value
        self.path = path

    def __repr__(self):
        return f'Op(pid={self.property_id!r} op={self.optype:d} name={self.name!r} path={self.path!r})'


class Tx:
    """一次 RPC 内累积的变更集。

    所有改动**同时**写进 props（服务端状态）和 ops（待发送帧），
    保证两边一致。
    """

    __slots__ = ("_added_paths", "_touched_roots", "ops", "props")

    def __init__(self, props):
        if not isinstance(props, dict):
            raise TxError(f"Tx 需要 props dict，收到 {type(props)!r}")
        self.props = props
        self.ops = []
        self._touched_roots = []
        # 本 Tx 里已经 OP_ADD 过的路径（用来避免「父帧里已经带着子对象，又给子发一条」）
        self._added_paths = []

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
            raise TxError(f"根属性 {root!r} 不存在，先 set 它")
        cur = self.props[root]
        cur_pid = cur.get("__id__") if isinstance(cur, dict) else None
        cur_type = sp.type if sp else None
        if not isinstance(cur_pid, int):
            raise TxError(f"根属性 {root!r} 没有 __id__，不能改它的子项")
        for i, key in enumerate(path[1:-1], start=1):
            if not isinstance(cur, dict):
                raise TxError(f'路径 {path!r} 第 {i:d} 段 {key!r} 的父不是 dict')
            if key not in cur:
                raise TxError(f'路径 {path!r} 第 {i:d} 段 {key!r} 不存在')
            cur = cur[key]
            nid = cur.get("__id__") if isinstance(cur, dict) else None
            if not isinstance(nid, int):
                raise TxError(f'路径 {path!r} 第 {i:d} 段 {key!r} 没有 __id__')
            cur_pid = nid
            # ★ 更新类型：先看「这个 key 是不是当前类型的结构体字段」，
            #   再看「当前类型是不是容器（取元素类型）」。
            #   审计 P1-3：之前只做 value_type(cur_type) or cur_type，
            #   下钻结构体字段时类型不更新 -> 4 层路径推出错误父类型 ->
            #   可能把新键误判成结构体字段发 OP_CHANGE。
            fields = _spec.struct_fields(cur_type) if cur_type else {}
            if key in fields:
                cur_type = fields[key][0]
            else:
                cur_type = _spec.value_type(cur_type) or cur_type
        return cur, path[-1], cur_pid, cur_type

    def _ensure_id(self, value):
        """值是新对象且没有 __id__ 时，分配一个。"""
        if isinstance(value, dict) and "__id__" not in value:
            value["__id__"] = _ids.alloc_id(self.props)
        return value

    def _check_value_ids(self, value, type_name, path):
        """结构校验：值子树里自定义类型节点必须带 __id__。

        ★ 为什么放在写入时（2026-10-26 两次「整号登不上」都在这里失守）：
          客户端 CustomDict/CustomList 的 roinit 是**无条件** assert(id ~= nil)。
          Tx._ensure_id 只给直接传给 set() 的顶层对象补 id，嵌套子对象要靠调用方。
          一次 set 塞多层新对象 -> 内层没 id -> 落盘 -> 客户端登录崩。
          放在这里，bug 在测试里当场红，而不是等玩家登录。
        """
        if type_name is None:
            return
        missing = []
        _validate.check_value(value, type_name, tuple(path), missing)
        if missing:
            first = "，".join(f"{p} ({t})" for p, t in missing[:3])
            raise TxError(
                f"路径 {path!r} 写入的对象缺 __id__：{first}；"
                "嵌套新建的自定义对象必须自己分配 id（property.ids.alloc_id）——"
                "客户端 CustomDict 会断言失败，导致 ClientAvatar:init 崩溃（整号登不上）")

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
                raise TxError(f"根属性 {key!r} 是 ServerOnly，不该推给客户端")
            self._ensure_id(value)
            self._check_value_ids(value, sp.type if sp else None, path)
            with write_guard("Tx.set"):
                parent[key] = value
            self._touch(key)
            self.ops.append(Op(0, OP_CHANGE, str(key), value, path))
            return value

        if not isinstance(parent, dict):
            raise TxError(f"路径 {path!r} 的父不是 dict")
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
        # ★ 写入即校验：值子树里凡是声明为自定义类型的节点都必须带 __id__。
        #   Tx 只给传进来的顶层对象补 id，嵌套子对象由调用方负责（契约边界）。
        vtype = None
        if is_struct_field:
            fdecl = fields.get(key)
            vtype = fdecl[0] if isinstance(fdecl, (list, tuple)) else fdecl
        elif parent_type:
            vtype = _spec.value_type(parent_type)
        self._check_value_ids(value, vtype, path)
        with write_guard("Tx.set"):
            parent[key] = value
        optype = OP_CHANGE if (is_struct_field or not is_new) else OP_ADD
        if optype == OP_ADD and self._covered_by_added(path):
            # 父容器本 Tx 里刚 OP_ADD 过，它那帧已经带着这个新对象了（活引用）
            return value
        self.ops.append(Op(pid, optype, _key_str(key), value, path))
        if optype == OP_ADD:
            self._added_paths.append(path)
        return value

    def delete(self, path):
        """删除容器里的一个键。

        ★ 两类禁止（2026-10-21 审计 P1-1）：
          1. 结构体字段不能删 —— 客户端 __OpDel__ 第一句就是
             assert(valueTypeDeclare ~= nil)，而结构体没有 __ValueTypeDeclare__ -> 崩
          2. list 类型的删除要用 OP_REMOVE（不是 OP_DEL）——
             客户端 __OpDel__ 里 assert(obj ~= nil) 也会崩
        """
        if isinstance(path, str):
            raise TxError("不能删除根属性")
        path = tuple(path)
        parent, key, pid, ptype = self._walk(path)
        if not isinstance(parent, dict):
            raise TxError(f"路径 {path!r} 的父不是 dict")
        fields = _spec.struct_fields(ptype) if ptype else {}
        if key in fields:
            raise TxError(f"不能删除结构体字段 {path!r}（客户端会 assert 崩）")
        if key not in parent:
            raise TxError(f"路径 {path!r} 的键不存在，不能删除")
        kind = _spec.key_kind(ptype) if ptype else None
        optype = OP_REMOVE if kind == "list" else OP_DEL
        del parent[key]
        self.ops.append(Op(pid, optype, _key_str(key), None, path))
        return True

    del_ = delete

    def insert(self, path, index, value):
        """往 list 容器的 index 位置插入一个元素（1-based），发 OP_INSERT。

        ★ 为什么要有这条 API（B 段遗留）：
          Tx 之前只有 set/delete，OP_INSERT **从来没被产出过** ——
          于是 list 类型属性只能整表替换，每加一个元素都要把整张表重发。

        客户端语义（puppetpropertycomponent.lua:329-375 __OpInsert__）：
          - 只对 list 有效：父对象必须有 __ValueTypeDeclare__，否则 :336 assert 崩
          - name 是 1-based 位置，客户端无条件 tonumber(name)
          - 越界（index > n+1）会让客户端 table.insert 崩
          - 插入后 index 之后的元素整体后移
        """
        path, container, pid, _ctype = self._list_container(path, index)
        keys = _normalize_list_keys(container)
        n = len(keys)
        idx = _as_index(index, "insert")
        if idx < 1 or idx > n + 1:
            raise TxError(f'insert 位置 {idx} 越界（当前 {n} 个元素，合法范围 1..{n + 1}）')
        if isinstance(value, dict):
            self._ensure_id(value)
        self._check_value_ids(value, _spec.value_type(_ctype), path + (idx,))
        # 从后往前挪，避免覆盖
        for i in range(n, idx - 1, -1):
            container[i + 1] = container.pop(i)
        container[idx] = value
        if not self._covered_by_added(path):
            self.ops.append(Op(pid, OP_INSERT, str(idx), value, path + (idx,)))
        return value

    def append(self, path, value):
        """往 list 末尾追加一个元素（insert 的语法糖）。"""
        if isinstance(path, str):
            path = (path,)
        else:
            path = tuple(path)
        container = self.get(path)
        if not isinstance(container, dict):
            raise TxError(f"路径 {path!r} 的容器不是 dict")
        keys = _normalize_list_keys(container)
        return self.insert(path, len(keys) + 1, value)

    def remove_at(self, path, index):
        """删除 list 的第 index 个元素（1-based），发 OP_REMOVE，后续元素前移。

        ★ 和 delete() 的区别：delete 是按**键**删（dict 语义），
          list 的键就是位置，所以两者对 list 等价；但 remove_at 做越界检查
          并按位置语义前移，调用方意图更明确。
        """
        path, container, pid, _ctype = self._list_container(path, index)
        keys = _normalize_list_keys(container)
        n = len(keys)
        idx = _as_index(index, "remove_at")
        if idx < 1 or idx > n:
            raise TxError(f'remove_at 位置 {idx} 越界（当前 {n} 个元素，合法范围 1..{n}）')
        old = container.pop(idx)
        for i in range(idx + 1, n + 1):
            container[i - 1] = container.pop(i)
        if not self._covered_by_added(path):
            self.ops.append(Op(pid, OP_REMOVE, str(idx), None, path + (idx,)))
        return old

    def _list_container(self, path, index):
        """定位 list 容器：返回 (path, container, container_pid, container_type)。"""
        if isinstance(path, str):
            path = (path,)
        else:
            path = tuple(path)
        container, _k, pid, ctype = self._walk(path + (index,))
        if not isinstance(container, dict):
            raise TxError(f"路径 {path!r} 的容器不是 dict")
        if _spec.key_kind(ctype) != "list":
            raise TxError(f"路径 {path!r} 的类型 {ctype!r} 不是 list（list 操作要用 set/insert）")
        return path, container, pid, ctype

    def _covered_by_added(self, path):
        """path 的某个祖先是否在本 Tx 里刚被 OP_ADD。

        ★ 为什么必须判断：OP_ADD 的 value 是 props 里的**活引用**，
          父容器建好之后再往里面塞子对象，父帧里已经带着那个子对象了；
          如果这时再给子对象发一条 OP_ADD，客户端会收到两遍同一个 __id__
          -> _attachToCache 打 "id repeated!" 并覆盖缓存，之后针对它的同步会打到旧对象上。
          （validate 的「__id__ 重复」检查也会报。）

        所以被祖先覆盖的写入**只改 props、不再发帧**。
        """
        for p in self._added_paths:
            if len(p) < len(path) and tuple(path[:len(p)]) == p:
                return True
        return False

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

        strict=True 时校验失败直接抛异常（测试/严格模式）；生产路径用 False。
        """
        from . import sync as _sync
        out = _sync.ops_to_frames(self.ops, strict=strict)
        self._check_reachable(strict=strict)
        return out

    def _check_reachable(self, strict=False):
        """每一帧都必须能在客户端 PropertyCallbacks 里找到接收方。

        ★ 为什么必须有这一层（2026-11-05 导演面板事故）：
          客户端按 optype 选回调表，再用**路径字符串精确查表**
          （puppetpropertycomponent.lua:91-110，无通配匹配）。
          查不到 = 静默丢弃：不报错、不重试、UI 不刷新。
          于是"帧确实发出去了"和"客户端知道了"是两件事，
          而只有重新登录（全量快照，逐键解析，不过回调）才能救回来。

          事故现场：解锁系统时先在 props 里 us[sid]=True 再 tx.set，
          被这里的 is_new 判成 OP_CHANGE，客户端拿 system_p.unlocked_system.*
          查表查不到 —— 83 个系统入口全都要重登才出现按钮。

        ★ 判据不手抄：路径算法和声明表都来自客户端（callback_contract.py），
          客户端改声明，这里自动跟着变。
        ★ 找不到客户端 config.lua 时 available() 为 False，本检查静默跳过 ——
          不能因为部署环境缺客户端文件就把服务判红。
        """
        from . import callback_contract as _cb

        bad = _cb.unreachable(self.ops)
        if not bad:
            return
        detail = "；".join(
            f"{_cb.OP_NAMES.get(op.optype, op.optype)}({key})" for op, key in bad[:5])
        more = "" if len(bad) <= 5 else f" …共 {len(bad)} 条"
        if strict:
            raise TxError(
                f"{len(bad)} 条帧客户端收不到（PropertyCallbacks 里没有接收方）："
                f"{detail}{more}。"
                "多半是「先直接改 props 再 tx.set」把 OP_ADD 判成了 OP_CHANGE —— "
                "删掉那行直接写，让 Tx 自己写值。")
        _log.warning(
            "[sync] ★ %d 条帧客户端收不到，UI 不会刷新（可能需重登才可见）：%s%s",
            len(bad), detail, more)

    def commit(self):
        """标记本次变更已写进 props（save 由调用方决定）。"""
        return len(self.ops)

    def __len__(self):
        return len(self.ops)

    def __repr__(self):
        return f'Tx({len(self.ops)} ops)'


def _as_index(index, where):
    """把位置参数转成 1-based 整数，非法直接抛（不要静默变 0）。"""
    if isinstance(index, bool):
        raise TxError(f"{where} 的位置不能是 bool")
    try:
        return int(index)
    except Exception:
        raise TxError(f"{where} 的位置 {index!r} 不是整数") from None


def _normalize_list_keys(container):
    """把 list 容器的位置键统一成整数键，返回排好序的位置列表。

    为什么会有两种形态：
      - "#N"：avatar_init.json 里就是这么写的；_apply_property 重建 list 时也写 "#N"
      - N   ：ms.conv_int_keys 之后的形态
    两种在发给客户端时都会转成整数（build_property_sync 里做），但服务端做位移时
    必须统一，否则 pop 和写回会互相看不见。
    """
    for k in [k for k in container
              if isinstance(k, str) and k.startswith("#") and k[1:].isdigit()]:
        container[int(k[1:])] = container.pop(k)
    return sorted(k for k in container
                  if isinstance(k, int) and not isinstance(k, bool))


def _key_str(key):
    """容器键转成帧里的 name 字段。

    规则（和客户端 CustomDict 一致）：
      - 字符串键：原样
      - 整数键：转成字符串（msgpack 里 name 是 string 字段）
    """
    return key if isinstance(key, str) else str(key)
