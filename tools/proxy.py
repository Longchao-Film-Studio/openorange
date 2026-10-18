#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools.proxy —— MITM 代理 (CONNECT 隧道)

独立启动:  python -m tools.proxy
监听:      0.0.0.0:4430

原理(已确证可行):
  FPX.dll 内嵌 libcurl, 支持 http_proxy/all_proxy 环境变量 + HTTPS CONNECT 隧道。
  客户端把 fp-passport.haowancheng.cn:443 经由本代理, 我们转给本地认证服。

用法:
  设环境变量后再启动游戏:
    set http_proxy=http://127.0.0.1:4430
    set all_proxy=http://127.0.0.1:4430
"""
import os
import socket
import sys
import threading

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOST = "0.0.0.0"
PORT = 4430

# 命中这些域名 -> 转到本地认证服; 其余直连(或拒绝)
TARGET_HOSTS = (
    "fp-passport.haowancheng.cn",
    "fp-passport-release.nenglianghe.cn",
    "account-release.nenglianghe.cn",
    "fp-idauth-release.nenglianghe.cn",
    "privacy-release.nenglianghe.cn",
)

# 服务器列表域名 -> 转到本地 serverlist 服务
# 客户端会 GET http://<host>/server_prod_simple.txt (纯 JSON)
SERVERLIST_HOSTS = (
    "l1-prod-serverlist-deep.haowancheng.cn",
    "l2-prod-serverlist-deep.haowancheng.cn",
    "l1-dev-serverlist-deep.haowancheng.cn",
    "l2-dev-serverlist-deep.haowancheng.cn",
)

LOCAL_SERVERLIST_HOST = "127.0.0.1"
LOCAL_SERVERLIST_PORT = 8080

LOCAL_AUTH_HOST = "127.0.0.1"
LOCAL_AUTH_PORT = 8443

# 非目标域名是否透传到真实服务器。
# True  = 补丁/CDN/其他服务照常联网(推荐, 游戏能正常启动)
# False = 拒绝(调试用, 便于发现漏网的域名)
PASSTHROUGH = os.environ.get("OO_NO_PASSTHROUGH") != "1"

BUF = 65536


def _pipe(a, b):
    try:
        while True:
            data = a.recv(BUF)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def _match(host, table):
    host = (host or "").split(":")[0].strip().lower()
    for t in table:
        if host == t or host.endswith("." + t):
            return True
    return False


def _is_target(host):
    return _match(host, TARGET_HOSTS)


def _is_serverlist(host):
    return _match(host, SERVERLIST_HOSTS)


def _handle(client):
    try:
        client.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(BUF)
            if not chunk:
                client.close()
                return
            head += chunk
            if len(head) > 65536:
                break

        first = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        parts = first.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            client.close()
            return

        hostport = parts[1]
        host = hostport.split(":")[0]

        if _is_serverlist(host):
            # 服务器列表 -> 本地 serverlist(返回 127.0.0.1:33100 的假服)
            print("[proxy] CONNECT %s -> local serverlist %s:%d"
                  % (hostport, LOCAL_SERVERLIST_HOST, LOCAL_SERVERLIST_PORT), flush=True)
            try:
                upstream = socket.create_connection(
                    (LOCAL_SERVERLIST_HOST, LOCAL_SERVERLIST_PORT), timeout=15)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
                threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
                return
            except Exception as e:
                print("[proxy] serverlist FAIL %s: %s" % (hostport, e), flush=True)
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                client.close()
                return

        if not _is_target(host):
            # 非目标域名: 透传到真实服务器(否则补丁/CDN/游戏服全断, 游戏进不去)
            port = 443
            if ":" in hostport:
                try:
                    port = int(hostport.rsplit(":", 1)[1])
                except ValueError:
                    port = 443
            if PASSTHROUGH:
                try:
                    upstream = socket.create_connection((host, port), timeout=15)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    print("[proxy] CONNECT %s -> passthrough" % hostport, flush=True)
                    threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
                    threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
                    return
                except Exception as e:
                    print("[proxy] passthrough FAIL %s: %s" % (hostport, e), flush=True)
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    client.close()
                    return
            print("[proxy] NOT-TARGET %s -> refused" % hostport, flush=True)
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            client.close()
            return

        print("[proxy] CONNECT %s -> local auth %s:%d"
              % (hostport, LOCAL_AUTH_HOST, LOCAL_AUTH_PORT), flush=True)
        upstream = socket.create_connection((LOCAL_AUTH_HOST, LOCAL_AUTH_PORT), timeout=15)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
    except Exception as e:
        print("[proxy] error:", e, flush=True)
        try:
            client.close()
        except Exception:
            pass


class ProxyServer:
    """极简包装: 暴露 serve_forever() 以便与 run_all 统一。"""

    def __init__(self, sock):
        self.sock = sock

    def serve_forever(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def serve(host=None, port=None):
    host = host or HOST
    port = port or PORT
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    print("[proxy] listening on %s:%d -> auth %s:%d"
          % (host, port, LOCAL_AUTH_HOST, LOCAL_AUTH_PORT), flush=True)
    return ProxyServer(srv)


def main():
    srv = serve()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[proxy] shutting down", flush=True)


if __name__ == "__main__":
    main()
