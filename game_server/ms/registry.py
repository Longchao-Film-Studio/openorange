#!/usr/bin/env python3
"""微服务方法分派表（唯一入口）。

## 为什么单独成模块

`transport/ms_service.py` 是**传输层**：它只负责切帧、解析 protobuf、写回 MsResponse。
业务模块（`mail/`、以后的 `shop/` …）要能登记自己的处理器，但不能反过来 import 传输层
—— 那会成环（ms_service import mail，mail import ms_service）。

所以这张表放在两者的**下面**：只依赖标准库，谁都能 import。

## 规则（都有测试）

    INV-MS-1  分派键是 (service, method)。同一个键只能有一个处理器 ——
              重复登记直接 raise（"两个服务有同名方法"这类事故在 import 期就炸）
    INV-MS-2  没登记的 (service, method) 一律按"未实现"处理（显式失败），
              不许有"按 method 猜测"的兜底

## 用法

    from ..ms import registry

    @registry.register("MailService", "getUserMailList")
    def _reply_user_mail_list(fields, cid): ...

处理器签名固定为 `(fields: dict[int, list], cid: int) -> bytes`：
`fields` 是 protobuf 解出来的 {字段号: [值...]}，`cid` 只用来打日志。
返回值是 MsResponse 的 msgpack 参数（**不含** Ret 以外的封装）。
"""
from __future__ import annotations

from collections.abc import Callable

#: (service, method) -> handler(fields, cid) -> msgpack bytes
Handler = Callable[[dict, int], bytes]

_HANDLERS: dict[tuple[str, str], Handler] = {}


def register(service: str, method: str) -> Callable[[Handler], Handler]:
    """把一个处理函数登记进分派表（装饰器）。重复登记 raise。"""

    def deco(fn: Handler) -> Handler:
        key = (service, method)
        old = _HANDLERS.get(key)
        if old is not None and old is not fn:
            raise ValueError(
                f"微服务方法 {service}.{method} 已经被 "
                f"{getattr(old, '__module__', '?')}.{getattr(old, '__name__', '?')} 占用")
        _HANDLERS[key] = fn
        return fn

    return deco


def resolve(service: str, method: str) -> Handler | None:
    """取处理器；没登记返回 None（调用方按"未实现"处理）。"""
    return _HANDLERS.get((service, method))


def table() -> dict[tuple[str, str], Handler]:
    """当前分派表的快照（测试用；返回副本，改它不影响真实分派）。"""
    return dict(_HANDLERS)


def registered_services() -> set[str]:
    """已经登记过方法的服务名集合。"""
    return {s for s, _ in _HANDLERS}
