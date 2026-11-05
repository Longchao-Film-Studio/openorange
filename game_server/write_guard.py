"""属性写入守卫 —— 所有属性写入的唯一闸门。

## 为什么需要

实机 bug（点「领取」经验不涨）的根因是「改了属性却没发同步帧」。
修完手工拼帧之后还需要一道防线：让「绕过 Tx 直接写 props」这件事
在代码层面可见、可查、可测。

## 两类写入，规则完全不同

登录期初始化（建新号骨架、补派生属性、跨天重置、分配 id）不该发帧，
那时客户端还没拿到 initDict。这类写入必须允许。

业务路径（RPC handler、事件链、心跳）的写入必须走 Tx，否则客户端看不到。

所以不能一刀切只读，必须显式区分：

    with write_guard("scaffold"):   # 初始化：显式声明豁免
        props["level"] = 1

    tx = Tx(props); tx.set(...)     # 业务：走 Tx（Tx 内部自己开守卫）

不在守卫里的直接写会抛 PropsWriteError，这是期望的失败：
它在测试里当场红，而不是等玩家重新登录才发现。

## 2026-11-06（B1）：从「只拦根属性」扩到「拦嵌套容器」

之前 GuardedProps 只拦根属性的直接替换，嵌套层拿到的是普通 dict，
于是一句 props["system_p"]["unlocked_system"][sid] = True 就绕过守卫。
「系统解锁要重登才生效」那个 bug 就是这么写出来的 —— 先直接改 props，
再 tx.set，Tx 看到键已存在，判成 OP_CHANGE，客户端回调查不到。

现在 GuardedDict / GuardedList 构造时把子容器递归包成同类，
**任何一层**的写入都要么在 write_guard 里，要么抛 PropsWriteError。

★ 关键取舍：它们仍然是 dict / list 的**子类**，不是代理对象。
  所以 isinstance(x, dict) / json.dump / msgpack / copy.deepcopy
  全部照常工作，359 处 isinstance(..., dict) 一处都不用改。

## 为什么用 with 而不是模块白名单

白名单会随时间腐化（新增文件忘了加、加了又不用）。
with 块把豁免限制在几行，读代码就能看到这里为什么可以直写。

## 只报告模式

    OO_GUARD_MODE=report

不抛异常，把违规写入的调用点记进 violations()。
用途是「一次拿到完整违规清单」—— 逐个修比逐个发现再修快得多。
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Any


class PropsWriteError(TypeError):
    """绕过 Tx 直接写属性。

    继承 TypeError，让 props 赋值报的错和别的类型问题属于同一类，
    调用方不用记两套异常。
    """


_LOCAL = threading.local()


def _depth() -> int:
    return getattr(_LOCAL, "depth", 0)


def in_guard() -> bool:
    """当前是否在 write_guard 里（Tx 也走这个）。"""
    return _depth() > 0


@contextlib.contextmanager
def write_guard(reason: str = ""):
    """显式声明这段代码可以直写 props。

    reason 只用于报错信息，不参与判断，但它强制调用方想清楚理由。
    """
    _LOCAL.reason = reason
    _LOCAL.depth = _depth() + 1
    try:
        yield
    finally:
        _LOCAL.depth = _depth() - 1
        if _depth() == 0:
            _LOCAL.reason = ""


#: 私有键前缀：`_oo_` 开头的是服务端内部簿记（当前副本 / 临时上下文 / id 计数器），
#: 客户端根本不知道它们存在，写它们**不发帧也不该拦**。
#: 这条规则让守卫只盯「真正的游戏属性」，不用给每处内部簿记都加豁免。
_PRIVATE_PREFIX = "_oo_"


#: 只报告模式（OO_GUARD_MODE=report）：不抛异常，把违规写入的位置记下来。
_GUARD_MODE_ENV = "OO_GUARD_MODE"
_VIOLATIONS: list = []


def _report_mode() -> bool:
    return os.environ.get(_GUARD_MODE_ENV, "").strip().lower() == "report"


def _record_violation(key: Any) -> None:  # noqa: ANN401
    """记一条违规：调用点文件/行/键。同一个位置只记一次。"""
    import traceback

    frame = None
    for st in traceback.extract_stack()[:-2]:
        if "write_guard.py" in st.filename:
            continue
        frame = st
    if frame is None:
        return
    rec = (frame.filename, frame.lineno, str(key))
    if rec not in _VIOLATIONS:
        _VIOLATIONS.append(rec)


def violations() -> list:
    """已记录的违规点（只报告模式下有效）。"""
    return list(_VIOLATIONS)


def reset_violations() -> None:
    _VIOLATIONS.clear()


_ALLOW_ENV = "OO_ALLOW_PROPS_WRITE"


def _env_allowed() -> bool:
    """测试/工具脚本可以整体放行（设 OO_ALLOW_PROPS_WRITE=1）。

    为什么留这个后门：测试要「造出某个状态」再验证行为，
    造状态本来就该能直接写属性（它不是在走业务路径）。
    生产代码永远不设这个变量，所以守卫对线上仍然生效。
    """
    return os.environ.get(_ALLOW_ENV, "") not in ("", "0", "false")


def _allowed(key: Any) -> bool:  # noqa: ANN401
    """这次写入是否放行：已在守卫里，或者写的是私有键。"""
    if _env_allowed() or in_guard() or str(key).startswith(_PRIVATE_PREFIX):
        return True
    if _report_mode():
        _record_violation(key)
        return True                     # 只报告模式：放过，但记下位置
    return False


def _wrap(value: Any) -> Any:  # noqa: ANN401
    """把 dict / list 递归包成 GuardedDict / GuardedList（其它原样返回）。

    ★ 返回的是**新对象**（内容浅拷贝 + 子容器递归包装）。
      因此只能在两个地方调用：
        · GuardedProps.__init__（加载存档 / 建号骨架 —— 整棵树一次性建好）
        · Tx.set（服务端 sanctioned 写入口，调用方不会留引用再改）

    ★★ 为什么 `__setitem__` 里**不能**用它（2026-11-06 实测事故）：
      业务代码普遍是「拿到子容器引用 -> 改它 -> st.set(根, 同引用)」，例如

          dslot = done.get(t)
          done[t] = dslot            # 若这里换成副本...
          dslot[task_id] = now       # ...这句就写进了孤儿对象
          st.set("done_tasks", done)

      实测症状：done_tasks[type][1] 只剩 {'__id__': 2885}，进度凭空丢失。
      所以 `__setitem__` 一律**存原对象**，靠静态检查
      （tests/test_nested_props_write.py）兜住「绕过守卫直接改嵌套」的写法。
    """
    if isinstance(value, (GuardedDict, GuardedList)):
        return value
    if isinstance(value, dict):
        return GuardedDict(value)
    if isinstance(value, list):
        return GuardedList(value)
    return value

class GuardedDict(dict):
    """受控的属性容器：守卫外不许直接写，**嵌套层也一样**。

    ★ 仍然是 dict 子类：isinstance(x, dict) / json.dump / msgpack / deepcopy
      全部照常工作，读取方不需要改任何代码。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__()
        if args or kwargs:
            # ★ 构造期整棵子树一次性包好（_wrap 会递归）。
            #   运行期的 __setitem__ 不这么做 —— 见 __setitem__ 的注释。
            for k, v in dict(*args, **kwargs).items():
                dict.__setitem__(self, k, _wrap(v))


    def __setitem__(self, key: Any, value: Any) -> None:  # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接写属性 props[{key!r}] 被拒绝：属性写入必须走 Tx。"
                "登录期初始化请用 with write_guard(理由) 显式豁免。",
            )
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: Any) -> None:  # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接删属性 props[{key!r}] 被拒绝：属性写入必须走 Tx。",
            )
        dict.__delitem__(self, key)

    def pop(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        _key = args[0] if args else None
        if not _allowed(_key):
            raise PropsWriteError(
                f"直接 pop 属性 props[{_key!r}] 被拒绝：属性写入必须走 Tx。",
            )
        return dict.pop(self, *args, **kwargs)

    def popitem(self) -> Any:  # noqa: ANN401
        if not _allowed(None):
            raise PropsWriteError("直接 popitem 被拒绝：属性写入必须走 Tx。")
        return dict.popitem(self)

    def update(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if not _allowed(None):
            raise PropsWriteError("直接 update 属性被拒绝：属性写入必须走 Tx。")
        for k, v in dict(*args, **kwargs).items():
            dict.__setitem__(self, k, _wrap(v))

    def setdefault(self, key: Any, default: Any = None) -> Any:  # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接 setdefault props[{key!r}] 被拒绝：属性写入必须走 Tx。",
            )
        if key not in self:
            dict.__setitem__(self, key, default)
        return dict.__getitem__(self, key)

    def clear(self) -> None:
        if not _allowed(None):
            raise PropsWriteError("直接 clear 属性被拒绝：属性写入必须走 Tx。")
        dict.clear(self)

    def __copy__(self):
        with write_guard("copy"):
            return GuardedDict(self)

    def __deepcopy__(self, memo):
        import copy as _copy

        with write_guard("deepcopy"):
            return GuardedDict(_copy.deepcopy(dict(self), memo))

    def __reduce__(self):
        # pickle / 某些序列化路径会走这里；还原成同样的类型
        return (GuardedDict, (dict(self),))


class GuardedList(list):
    """受控的列表容器：守卫外不许直接写。

    列表没有键，所以判据只能看 in_guard()。
    ★ 仍是 list 子类：isinstance(x, list) / json.dump 照常工作。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__()
        if args or kwargs:
            items: list = list(*args, **kwargs)
            for item in items:
                list.append(self, _wrap(item))


    def _deny(self, what: str) -> None:
        if not _allowed(None):
            raise PropsWriteError(
                f"直接 {what} 列表属性被拒绝：属性写入必须走 Tx。",
            )

    def __setitem__(self, key, value):
        self._deny("写")
        list.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._deny("删")
        list.__delitem__(self, key)

    def append(self, value):
        self._deny("append")
        list.append(self, value)

    def extend(self, values):
        self._deny("extend")
        list.extend(self, values)

    def insert(self, index, value):
        self._deny("insert")
        list.insert(self, index, value)

    def pop(self, *args):
        self._deny("pop")
        return list.pop(self, *args)

    def remove(self, value):
        self._deny("remove")
        list.remove(self, value)

    def clear(self):
        self._deny("clear")
        list.clear(self)

    def sort(self, *args, **kwargs):
        self._deny("sort")
        list.sort(self, *args, **kwargs)

    def reverse(self):
        self._deny("reverse")
        list.reverse(self)

    # 注：故意不实现 __iadd__ —— 它会和 list.__add__ 的签名冲突（mypy misc）。
    # list 的 += 走 __add__，结果是新 list（不带守卫），属于已知边界：
    # 业务代码不该对属性列表用 +=，要改就走 Tx.insert / Tx.append。

    def __copy__(self):
        with write_guard("copy"):
            return GuardedList(self)

    def __deepcopy__(self, memo):
        import copy as _copy

        with write_guard("deepcopy"):
            return GuardedList(_copy.deepcopy(list(self), memo))

    def __reduce__(self):
        return (GuardedList, (list(self),))


#: 旧名字（player_state.py 等仍在 import），指向同一个实现。
GuardedProps = GuardedDict
