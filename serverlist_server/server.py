#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serverlist_server —— 假服务器列表服务

独立启动:  python -m serverlist_server.server
监听:      0.0.0.0:8080   (客户端通过代理访问)

客户端行为(已由 Lua 反编译确证):
  GET https://l1-prod-serverlist-deep.haowancheng.cn/server_prod_simple.txt
  -> 期望返回 JSON: { "servers": [...], "regions": [...], "clientGate": "..." }

我们返回一份只含"本地服"的列表, clientGate 指向 game_server。
"""
import json
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_server import config as game_config  # noqa: E402

HOST = "0.0.0.0"
PORT = 8080

# 客户端通过 HTTPS CONNECT 隧道访问, 所以本服务也要用 TLS
# (证书复用 auth_server 的 tools/cert/server.crt)
CERT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "cert")
CERT_FILE = os.path.join(CERT_DIR, "server.crt")
KEY_FILE = os.path.join(CERT_DIR, "server.key")

RID = 10          # 地区 id(沿用游戏自带 local_server_list 的值)
HOSTNUM = 2000    # 服务器编号(同上)


def build_server_list():
    """生成服务器列表 JSON。"""
    host = getattr(game_config, "ADVERTISE_HOST", "127.0.0.1")
    gate = "%s:%d" % (host, game_config.PORT)
    raw_clusters = getattr(game_config, "CLUSTERS", None) or []
    clusters = []
    for c in raw_clusters:
        if isinstance(c, dict):
            clusters.append({"addrs": c.get("addrs", []),
                             "clusterId": c.get("clusterId", game_config.CLUSTER_ID),
                             "status": 1})
        else:
            clusters.append({"addrs": [], "clusterId": c, "status": 1})
    if not clusters:
        clusters = [{"addrs": [], "clusterId": game_config.CLUSTER_ID, "status": 1}]

    servers = [{
        "hostnum": HOSTNUM,
        "name": "OpenOrange 本地服",
        "clientGate": gate,
        "status": 1,
        "addrs": [],
        "openTime": 0,
        "rid": RID,
        "clusters": clusters,
        "ms_gate": gate,
    }]

    regions = [{
        "name": "本地",
        "rid": RID,
        "type": 1,
        "clientGate": gate,
        "servers": servers,
    }]

    return {
        "servers": servers,
        "regions": regions,
        "clientGate": gate,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith(".txt") or path in ("/", "/index.html"):
            data = build_server_list()
            print("[serverlist] %s -> clientGate=%s" % (path, data["clientGate"]), flush=True)
            self._send(json.dumps(data, ensure_ascii=False))
            return
        self._send("not found", "text/plain; charset=utf-8", 404)


def serve(host=None, port=None):
    host = host or HOST
    port = port or PORT
    httpd = ThreadingHTTPServer((host, port), Handler)
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print("[serverlist] https://%s:%d (server list, TLS)" % (host, port), flush=True)
    else:
        print("[serverlist] http://%s:%d (server list, 无证书降级为明文)"
              % (host, port), flush=True)
    return httpd


def main():
    httpd = serve()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[serverlist] shutting down", flush=True)


if __name__ == "__main__":
    main()