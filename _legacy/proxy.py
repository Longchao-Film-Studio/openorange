#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 代理转发层: 拦截发往 fp-passport.haowancheng.cn 的请求, 转发到本地假服务端 fake_server。
# 两种模式:
#   MITM: 监听 PROXY_PORT, 客户端把该端口设为代理(HTTP CONNECT), 对目标域名在本地应答。
#   HOSTS: 直接改 hosts 把目标域名指到 127.0.0.1, 本地起 443 的假服务端(见 cert/run)。
# 本文件实现 MITM 转发代理; 说明见 README。
import socket, threading, select, ssl, sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

class Proxy:
    def __init__(self, port=config.PROXY_PORT):
        self.port = port
        self.ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind((config.HOST, port))
        self.ls.listen(64)
    def run(self):
        print("[proxy] MITM listening on", self.port, flush=True)
        while True:
            c, _ = self.ls.accept()
            threading.Thread(target=self._client, args=(c,), daemon=True).start()
    def _client(self, c):
        try:
            # 读 HTTP CONNECT 行
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d: return
                buf += d
            head = buf.split(b"\r\n", 1)[0].decode(errors="ignore")
            parts = head.split()
            # CONNECT host:port
            if parts and parts[0].upper() == "CONNECT":
                hostport = parts[1]
                host = hostport.rsplit(":", 1)[0]
                c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                print("[proxy] CONNECT", host, flush=True)
                if host == config.TARGET_HOST or host.endswith(config.TARGET_HOST.lstrip("*")):
                    # 本地假服务端
                    print("[proxy] -> local fake server for", host, flush=True)
                    self._relay(c, config.HOST, config.SERVER_PORT)
                else:
                    # 其他域名走真实(此处仅演示日志; 完整实现需外连)
                    print("[proxy] passthrough", host, "(not implemented, closing)", flush=True)
                    c.close()
            else:
                c.close()
        except Exception as e:
            print("[proxy] err", repr(e), flush=True)
            try: c.close()
            except: pass
    def _relay(self, client, rhost, rport):
        up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        up.connect((rhost, rport))
        socks = [client, up]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 1.0)
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (up if s is client else client).sendall(data)
        finally:
            try: client.close()
            except: pass
            try: up.close()
            except: pass

if __name__ == "__main__":
    Proxy().run()
