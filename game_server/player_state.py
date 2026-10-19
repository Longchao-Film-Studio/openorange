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
_STATE_DIR = (os.environ.get("OO_STATE_DIR")
               or os.path.join(os.path.dirname(_HERE), "work", "player_state"))

_LOCK = threading.RLock()
_CURRENT = None

DEFAULT_UID = "OO00000001"


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

    # ---- 读写 ----
    def get(self, name, default=None):
        with _LOCK:
            return self.props.get(name, default)

    def set(self, name, value):
        with _LOCK:
            self.props[name] = value
            return value

    def next_property_id(self):
        """属性同步的序号，客户端用来去重/排序。"""
        with _LOCK:
            self.property_seq += 1
            return self.property_seq

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
        return st
    except Exception:
        return None


def get_state(init_props=None, uid=DEFAULT_UID):
    """取当前玩家状态；第一次调用时优先读盘，没有盘就用 init_props 建一份。"""
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            st = load(uid)
            if st is None:
                st = PlayerState(uid=uid, props=copy.deepcopy(init_props or {}))
            _CURRENT = st
        return _CURRENT


def reset_state(init_props=None, uid=DEFAULT_UID):
    global _CURRENT
    with _LOCK:
        _CURRENT = PlayerState(uid=uid, props=copy.deepcopy(init_props or {}))
        return _CURRENT


def save_state():
    with _LOCK:
        if _CURRENT is None:
            return False
        return _CURRENT.save()
