#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# OpenOrange 一键启动: 假服务端(fake_server) + MITM 代理(proxy)。
# 用法: python run.py
import sys, os, threading, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

def main():
    # 确保证书存在
    if not (os.path.exists(config.CERTFILE) and os.path.exists(config.KEYFILE)):
        print("[run] generating certs..."); subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_cert.py")])
    # 启动假服务端
    import fake_server
    t1 = threading.Thread(target=fake_server.serve, daemon=True)
    t1.start()
    # 启动代理
    import proxy
    t2 = threading.Thread(target=proxy.Proxy().run, daemon=True)
    t2.start()
    print("[run] OpenOrange running.")
    print("[run] fake server  : https://127.0.0.1:%d  (fp-passport)" % config.SERVER_PORT)
    print("[run] MITM proxy   : 127.0.0.1:%d" % config.PROXY_PORT)
    print("[run] 下一步: 让客户端连到本地(改hosts 或 指代理), 并信任 cert/ca.crt")
    while True:
        try:
            import time; time.sleep(3600)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
