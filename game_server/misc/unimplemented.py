#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端发、服务端只需静默或只回 status=true 的 RPC（P0-E2 第 1 步）。

分两类：
  _ack_only      带回调的 RPC，但服务端没有状态要改，回 status=true 即可；
                 三个组件里所有 _SC 第一句都是 if retstatus.status then ...
  _noop_one_way  单向消息（serverMsg / CallServer，rid=0），连应答都不能回
                 （回了客户端报 "entityMessageResponse got rid 0"）。
"""
from .. import ms as msmod
from ..rpc_index import ACK_OK

def _ack_only(rid, params, uid=None):
    """只回 status=true 的 RPC。"""
    return [msmod.build_entity_response(rid, ret=dict(ACK_OK))]


def _noop_one_way(params, uid=None):
    """单向无操作（RpcEnterTargetSeq / 战斗失败统计 / 加载进度）。"""
    return []
