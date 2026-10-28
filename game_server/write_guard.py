"""属性写入守卫。

##为什么需要它

实机 bug（点「领取」经验不涨）的根因是「改了属性却没发同步帧」。
修完手工拼帧之后还需要一道防线：让「绕过 Tx直接写 props」这件事
在代码层面可见、可查、可测。

##两类写入，规则完全不同

登录期初始化（建新号骨架、补派生属性、跨天重置、分配 id）不应该发帧，
那时客户端还没拿到 initDict。这类写入必须允许。

业务路径（RPC handler、事件链、心跳）的写入必须走 Tx，否则客户端看不到。

所以不能一刀切只读，必须显式区分：

 with write_guard("scaffold"): #初始化：显式声明豁免
 props["level"] =1

 tx = Tx(props); tx.set(...) #业务：走 Tx（Tx内部自己开守卫）

不在守卫里的直接写会抛 PropsWriteError，这是期望的失败：
它在测试里当场红，而不是等玩家重新登录才发现。

##为什么用 with而不是模块白名单

白名单会随时间腐化（新增文件忘了加、加了又不用）。
with块把豁免限制在几行，读代码就能看到这里为什么可以直写。
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Any


class PropsWriteError(TypeError):
    """绕过 Tx直接写属性。

    继承 TypeError，让 props赋值报的错和别的类型问题属于同一类，
    调用方不用记两套异常。
    """


_LOCAL = threading.local()


def _depth() -> int:
    return getattr(_LOCAL, "depth",0)


def in_guard() -> bool:
    """当前是否在 write_guard里（Tx也走这个）。"""
    return _depth() >0


@contextlib.contextmanager
def write_guard(reason: str = ""):
    """显式声明这段代码可以直写 props。

    reason只用于报错信息，不参与判断，但它强制调用方想清楚理由。
    """
    _LOCAL.reason = reason
    _LOCAL.depth = _depth() +1
    try:
     yield
    finally:
     _LOCAL.depth = _depth() -1
     if _depth() ==0:
        _LOCAL.reason = ""


#:私有键前缀：`_oo_`开头的是服务端内部簿记（当前副本 /临时上下文 / id计数器），
#:客户端根本不知道它们存在，写它们**不发帧也不该拦**。
#:这条规则让守卫只盯「真正的游戏属性」，不用给每处内部簿记都加豁免。
_PRIVATE_PREFIX = "_oo_"


_ALLOW_ENV = "OO_ALLOW_PROPS_WRITE"


def _env_allowed() -> bool:
 """测试/工具脚本可以整体放行（设 OO_ALLOW_PROPS_WRITE=1）。

 为什么留这个后门：测试要「造出某个状态」再验证行为，
 造状态本来就该能直接写属性（它不是在走业务路径）。
 生产代码永远不设这个变量，所以守卫对线上仍然生效。
 """
 return os.environ.get(_ALLOW_ENV, "") not in ("", "0", "false")


def _allowed(key: Any) -> bool: # noqa: ANN401
 """这次写入是否放行：已在守卫里，或者写的是私有键。"""
 return _env_allowed() or in_guard() or str(key).startswith(_PRIVATE_PREFIX)


class GuardedProps(dict):
    """受控的属性容器：守卫外不许直接写。

    只拦「写根属性」，不拦读，也不拦改子对象。
    改子对象仍然能绕过去，这是已知边界，
    由 Tx的变更集 +一致性校验器兜底（见 docs/方案-属性同步一致性.md）。
    拦根属性的直接替换，是因为那是最常见的形态，也是本次实机 bug的形态。
    """

    def __setitem__(self, key: Any, value: Any) -> None: # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接写属性 props[{key!r}]被拒绝：属性写入必须走 Tx。"
                "登录期初始化请用 with write_guard(理由)显式豁免。",
            )
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None: # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接删属性 props[{key!r}]被拒绝：属性写入必须走 Tx。",
            )
        super().__delitem__(key)

    def pop(self, *args: Any, **kwargs: Any) -> Any: # noqa: ANN401
        # pop的第一个位置参数就是 key：私有键放行，其余要求进守卫。
        _key = args[0] if args else None
        if not _allowed(_key):
         raise PropsWriteError(f"直接 pop属性 props[{_key!r}]被拒绝：属性写入必须走 Tx。")
        return super().pop(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> None: # noqa: ANN401
        #批量操作/无 key的调用：只看是否在守卫里
        if not in_guard():
            raise PropsWriteError("直接 update属性被拒绝：属性写入必须走 Tx。")
        super().update(*args, **kwargs)

    def setdefault(self, key: Any, default: Any = None) -> Any: # noqa: ANN401
        if not _allowed(key):
            raise PropsWriteError(
                f"直接 setdefault props[{key!r}]被拒绝：属性写入必须走 Tx。",
            )
        return super().setdefault(key, default)

    def clear(self) -> None:
        #批量操作/无 key的调用：只看是否在守卫里
        if not in_guard():
            raise PropsWriteError("直接 clear属性被拒绝：属性写入必须走 Tx。")
        super().clear()
