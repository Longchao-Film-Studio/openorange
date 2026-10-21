#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家可变状态（阶段 1 的服务端权威数据）。

为什么需要它
------------
在这之前所有数据都来自 game_server/avatar_init.json 的静态初始值：
客户端能读到（探针验证过），但服务端**改不了**，也没有地方存。
Set* 类 RPC 只能回一个假的 status=true，等于假成功。

这一层把玩家数据变成服务端权威的可变状态：
  - 登录时从这里取 initDict；
  - Set* RPC 改这里的值；
  - 改完推一条 propertySync2Client 让客户端同步。

持久化
------
落到 work/player_state/<uid>.json，重启不丢。写入用「临时文件 + 替换」，
避免写一半断电把存档写坏。
"""
import copy
import json
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
# 默认 work/player_state；设 OO_STATE_DIR 可换目录。
# ★ 测试必须设成临时目录，否则会覆盖真实玩家存档
#   （test_property_sync.py 用默认 uid 跑 reset_state + save，踩过一次）。
# 存档目录统一由 config 管：默认 work/player_state，设 OO_STATE_DIR 可换。
from . import config as _cfg  # noqa: E402
from .property import ids as _ids  # noqa: E402
_STATE_DIR = _cfg.STATE_DIR

_LOCK = threading.RLock()
# 每个账号一份状态：uid -> PlayerState。
# 以前只有一个 _CURRENT，多开时所有客户端会共用同一份数据。
_STATES = {}

# 兜底 uid（账号关联失败时才用）。正常路径 uid = 登录名。
DEFAULT_UID = _cfg.FALLBACK_UID


def _keys_to_json(obj):
    """把整数键转成 "#<int>" 再写 JSON。

    ★ JSON 的对象键只能是字符串，直接 dump 会把整数键 1 变成 "1"。
    客户端 CustomDict 的 __IntTypeKey__ 类型要求真整数键，收到字符串键会崩，
    表现是连接立刻断开。所以存档必须用 avatar_init.json 同款 "#1" 约定。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, int):
                out["#%d" % k] = _keys_to_json(v)
            else:
                out[k] = _keys_to_json(v)
        return out
    if isinstance(obj, list):
        return [_keys_to_json(v) for v in obj]
    return obj


def _keys_from_json(obj):
    """把 "#<int>" 转回整数键。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if (isinstance(k, str) and k.startswith("#")
                    and k[1:].lstrip("-").isdigit()):
                out[int(k[1:])] = _keys_from_json(v)
            else:
                out[k] = _keys_from_json(v)
        return out
    if isinstance(obj, list):
        return [_keys_from_json(v) for v in obj]
    return obj


class PlayerState(object):
    def __init__(self, uid=DEFAULT_UID, props=None):
        self.uid = uid
        self.props = props if props is not None else {}
        self.property_seq = 0
        # 是否「本次新建」而不是从存档读出来的（见 get_state 注释）
        self.is_new = False

    # ---- 读写 ----
    def get(self, name, default=None):
        with _LOCK:
            return self.props.get(name, default)

    def set(self, name, value):
        with _LOCK:
            self.props[name] = value
            return value

    def next_property_id(self):
        """分配一个新的属性对象 __id__。

        ★ 2026-10-21 审计：这里以前和 `story._next_id_for_props()` 是**两个**
          互不相知的分配器，实测撞号（1381 同时属于背包格子和任务条目）。
          现在统一委托给 `property.ids.alloc_id()`，全项目唯一入口。

        注意：分配进度存在 `props["_oo_next_id"]`，不再用 `property_seq`。
        `property_seq` 只留给属性同步的序号（见 `_unused_next_property_id`）。
        """
        return _ids.alloc_id(self.props)

    def to_dict(self):
        with _LOCK:
            return {
                "uid": self.uid,
                "props": _keys_to_json(copy.deepcopy(self.props)),
                "property_seq": self.property_seq,
            }

    @classmethod
    def from_dict(cls, d):
        st = cls(d.get("uid") or DEFAULT_UID, _keys_from_json(d.get("props") or {}))
        try:
            st.property_seq = int(d.get("property_seq") or 0)
        except Exception:
            st.property_seq = 0
        return st

    # ---- 持久化 ----
    def path(self):
        return os.path.join(_STATE_DIR, "%s.json" % self.uid)

    def save(self):
        d = self.to_dict()
        p = self.path()
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            return True
        except Exception:
            return False


def _migrate_item_bag_keys(props):
    """把 item_bag 的整数键统一转成字符串键。

    ★ 为什么（2026-10-21 审计 P0-5）：
      ItemBag 的 __IntTypeKey__ = false，客户端是 item_bag[gridId] **字符串**查询。
      旧存档里 JSON 的 "#1" 读回来会变成整数 1，于是 bag 里同时有 "1" 和 1。
      后果：
        - 发奖命中整数键 -> 同步 name="1" -> 客户端 "1" 格是别的道具 ->
          __id__ 不匹配 -> 顶掉客户端第 1 格（奖励实际是丢的）
        - UseItem_CS 对整数键 -> ms.pb_bytes_field 里 len(int) -> TypeError

    返回迁移了几个。
    """
    bag = props.get("item_bag")
    if not isinstance(bag, dict):
        return 0
    int_keys = [k for k in bag
                if isinstance(k, int) and not isinstance(k, bool)]
    if not int_keys:
        return 0
    n = 0
    for k in int_keys:
        grid = bag.pop(k)
        sk = str(k)
        if sk in bag:
            # 冲突：整数 k 和字符串 str(k) 同时存在 -> 换一个空键
            nk = k
            while str(nk) in bag:
                nk += 1000
            sk = str(nk)
        bag[sk] = grid
        if isinstance(grid, dict):
            grid["id"] = sk
        n += 1
    if n:
        print("[state] item_bag 迁移了 %d 个整数键 -> 字符串键" % n,
              flush=True)
    return n


def load(uid=DEFAULT_UID):
    """读盘；没有存档就返回 None。"""
    p = os.path.join(_STATE_DIR, "%s.json" % uid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            st = PlayerState.from_dict(json.load(f))
        # 存档损坏/格式不对时不要硬用：属性太少说明写坏了，回退到初始值
        if not isinstance(st.props, dict) or len(st.props) < 10:
            return None
        # ★ 校正 __id__ 分配计数器 + 检测已有撞号（审计 P0-2/P0-3）
        _ids.reconcile(st.props)
        # ★ 旧存档的 item_bag 整数键迁移成字符串键（审计 P0-5）
        _migrate_item_bag_keys(st.props)
        return st
    except Exception:
        return None


def get_state(init_props=None, uid=DEFAULT_UID):
    """取某个账号的玩家状态；第一次调用时优先读盘，没有盘就用 init_props 建一份。

    多账号：按 uid 分开缓存，各自读自己的 <uid>.json。
    """
    uid = uid or DEFAULT_UID
    with _LOCK:
        st = _STATES.get(uid)
        if st is None:
            st = load(uid)
            if st is None:
                st = PlayerState(uid=uid, props=copy.deepcopy(init_props or {}))
                # ★ 标记「这是新建的存档」，不是从盘里读出来的。
                #   新号初始化（脚手架、体力拉满）只能对新建存档做一次；
                #   以前用内存标记，进程一重启就丢，导致每次登录都把
                #   玩家的体力重置成满值、脚手架重新应用一遍。
                st.is_new = True
            else:
                st.is_new = False
            _STATES[uid] = st
        return st


def peek(uid=DEFAULT_UID):
    """只看状态，不存在就返回 None（**不创建**）。

    为什么要它：`get_state()` 在没有存档时会建一份空白状态并缓存。
    如果管理接口的**查询**命令用了 get_state，就会留下一个空壳存档，
    导致后面真正的登录看到 is_new=False、跳过新号初始化（任务 1000 就没了）。
    """
    uid = uid or DEFAULT_UID
    with _LOCK:
        st = _STATES.get(uid)
        if st is not None:
            return st
        st = load(uid)
        if st is None:
            return None
        st.is_new = False
        _STATES[uid] = st
        return st


def reset_state(init_props=None, uid=DEFAULT_UID):
    uid = uid or DEFAULT_UID
    with _LOCK:
        _STATES[uid] = PlayerState(uid=uid, props=copy.deepcopy(init_props or {}))
        return _STATES[uid]


def save_state(uid=None):
    """保存状态。uid 为 None 时保存全部。"""
    with _LOCK:
        if uid is not None:
            st = _STATES.get(uid)
            return st.save() if st is not None else False
        ok = True
        for st in _STATES.values():
            ok = st.save() and ok
        return ok


def list_states():
    """当前缓存里有哪些账号。"""
    with _LOCK:
        return sorted(_STATES.keys())
