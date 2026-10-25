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

    如果 street_info[street_id] 不存在，用 Tx 建（自动 OP_ADD），
    避免审计里「新建条目用 OP_CHANGE -> 客户端 oldVal nil 崩」的坑。

    is_new=True 时，调用方**只需改 entry 的内容**，不要再对 entry 的字段
    单独 tx.set —— 因为那条 OP_ADD 帧里带的就是改完的 entry。
    """
    tx = _tx.Tx(st.props)
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return tx, None, None, False
    if not isinstance(si.get(street_id), dict):
        tx.set(("street_info", street_id), _make_street_info_entry(st))
        si = st.props.get("street_info")
        return tx, si, si.get(street_id), True
    return tx, si, si.get(street_id), False

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
    """取 street_info[street_id]，没有就建（直接插入 props）。

    ★ 条目一律由 _make_street_info_entry 构造。审计 P2-4：以前这里把默认字段和
      5 个嵌套 __id__ 的分配逻辑又抄了一遍，和 _make_street_info_entry 各写各的，
      改一处忘一处就会漂移（两种 __id__ 分配写法各写一遍）。

    ★ 需要发同步帧的路径不要用这个函数，改用 _make_street_info_entry() + Tx，
      让 Tx 自己判断 OP_ADD —— 先插入再交给 Tx 会发成 OP_CHANGE，客户端崩。
    """
    si = st.props.get("street_info")
    if not isinstance(si, dict):
        return None, None
    cur = si.get(street_id)
    if isinstance(cur, dict):
        return si, cur
    entry = _make_street_info_entry(st)
    si[street_id] = entry
    st.set("street_info", si)
    return si, entry
