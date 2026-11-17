#!/usr/bin/env python3
"""街区条目（street_info）基础设施（P0-E2 第 4 步）。

street_info 是 StreetInfoDict（generated/config.lua:6834/6928），客户端多处
getStreetInfo() 直接取 self.street_info[street_id]，nil 会崩，所以任何改街区的
路径都要先保证条目存在。

★ 为什么拆出「只构造不插入」的 _make_street_info_entry：
  Tx.set 靠「键在不在父容器里」判断 OP_ADD / OP_CHANGE。
  如果先插入再交给 Tx，Tx 会以为键已存在 -> 发 OP_CHANGE ->
  客户端 oldVal:customTypeName() 索引 nil -> 同步被丢（审计实锤）。
"""
from ..property import tx as _tx


def _street_tx_begin(st, street_id):
    """开始一个街区变更：返回 (tx, si, entry, is_new)。

    ★ 契约（2026-11-17 重写）：**本函数不写 props**，只准备对象。

        is_new=False  entry 是 props 里的容器 —— **不要直接改它**
                      （那是裸写，守卫会抛：主线 1007 卡死就是这么来的）
        is_new=True   entry 是**尚未插入 props 的副本**

    两条路的写法一样：先 mutable(entry) 取纯副本，改完再 tx.set 落地。
    Tx 靠「键在不在父容器里」自己判 OP_ADD / OP_CHANGE，所以：
        is_new  -> tx.set(("street_info", sid), new_entry)          # OP_ADD
        否则    -> tx.set(("street_info", sid, field), new_value)   # OP_CHANGE
    绝不能先插进 props 再交给 Tx（会发成 OP_CHANGE，客户端 oldVal nil 崩）。
    """
    tx = _tx.Tx(st.props)
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return tx, None, None, False
    cur = si.get(street_id)
    if isinstance(cur, dict):
        return tx, si, cur, False
    return tx, si, _make_street_info_entry(st), True

def _make_street_info_entry(st):
    """构造一个 StreetInfo 条目（**不插入** props）。

    字段由 property.factory 按 spec_data.json 里的 StreetInfo 声明生成
    （11 个字段，5 个嵌套容器各分自己的 __id__），空容器客户端读时都有 or {} 兜底。

    为什么要拆出「只构造不插入」：
      Tx.set 靠「键在不在父容器里」判断 OP_ADD / OP_CHANGE。
      如果先插入再交给 Tx，Tx 会以为键已存在 -> 发 OP_CHANGE ->
      客户端 oldVal:customTypeName() 索引 nil -> 同步被丢（审计实锤）。
    """
    from ..property import factory as _factory
    return _factory.new_instance(st.props, "StreetInfo", with_tp=True)

def _street_info_of(st, street_id):
    """取 street_info[street_id]；没有就返回一个**尚未插入 props 的副本**。

    ★ 与 _street_tx_begin 同一契约：本函数不写 props。
      调用方拿到 entry 后一律 mutable(entry) 改副本，再用 Tx 落地。
      （旧版在这里 si[street_id] = entry 直接插进 props —— 那是裸写，
        会让守卫抛异常；而且先插入再交给 Tx 会发成 OP_CHANGE，客户端崩。）

    ★ 条目一律由 _make_street_info_entry 构造（审计 P2-4：字段和 __id__
      分配只允许有一处实现）。
    """
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return None, None
    cur = si.get(street_id)
    if isinstance(cur, dict):
        return si, cur
    return si, _make_street_info_entry(st)
