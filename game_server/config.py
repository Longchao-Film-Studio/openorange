#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.config —— 游戏服配置(占位)"""

HOST = "0.0.0.0"
PORT = 33100                # clientGate, 对应 local_server_list.lua 的 127.0.0.1:33100
CLUSTER_ID = 20000

# 下发给客户端的地址(serverlist 里 clientGate / clusters.addrs 用)
ADVERTISE_HOST = "127.0.0.1"

# 后续多子服: 这里可以扩展成 cluster 列表
CLUSTERS = [
    {"clusterId": 20000, "addrs": ["127.0.0.1:20000", "127.0.0.1:20100"]},
]
