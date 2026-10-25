#!/usr/bin/env python3
"""avataruserdatacomp 的 batchGetAttribute 应答（P0-E3 从 ms.py 拆出）。

客户端 entities/avatarcomponent/avataruserdatacomp.lua:151 用这个通道批量取
玩家数据（签名 / 头像 / 称号 / 各 bag 的数量等）。请求是一组属性名，
应答是 {Results: [{Uid, AttributesMap}, ...]}，值只支持简单类型。

★ 只回简单类型（str/int/float/bool）。自定义类型（skins、char_list 等）在
  initDict 里是 {__tp__, __id__} 这种占位表示，不是客户端要的真实结构，
  硬塞会喂错数据，所以跳过 —— 客户端对缺失属性本来就是容忍的。
"""
from .defaults import _DEF_UID
from .protocol import mp_map

# 能直接塞进 msgpack 的属性值类型
_SIMPLE_TYPES = (str, int, float, bool)


def build_userdata_batch_params(request_params, uid_default=None):
    """构造 batchGetAttribute 的应答参数（msgpack map）。

    请求结构：parameters = [ list ]，list = [ [uidStr, [attr, ...]], ... ]
    应答结构：{ Ret = [status, errmsg], Results = [ {Uid, AttributesMap} ] }
    """
    # 延迟导入：AVATAR_INIT_DICT 在包 __init__ 里加载，顶层导入会循环依赖。
    from . import AVATAR_INIT_DICT

    uid_default = uid_default or _DEF_UID
    props = AVATAR_INIT_DICT.get("__Properties__", {}) or {}
    reqs = []
    if isinstance(request_params, list) and request_params:
        first = request_params[0]
        if isinstance(first, list):
            reqs = first
    results = []
    for item in reqs:
        if not isinstance(item, list) or len(item) < 2:
            continue
        u = item[0]
        names = item[1] if isinstance(item[1], list) else []
        amap = {}
        for nm in names:
            if not isinstance(nm, str):
                continue
            if nm in props and isinstance(props[nm], _SIMPLE_TYPES):
                amap[nm] = props[nm]
        results.append({"Uid": u if isinstance(u, str) else str(u),
                        "AttributesMap": amap})
    if not results:
        results.append({"Uid": uid_default, "AttributesMap": {}})
    return mp_map({"Ret": [True, 0], "Results": results})
