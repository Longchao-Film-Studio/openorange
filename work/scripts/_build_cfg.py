import os
base='F:/haowancheng/OpenOrange'
os.makedirs(base,exist_ok=True)
def w(name,text):
    open(os.path.join(base,name),'w',encoding='utf-8').write(text)
# config.py
cfg='''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenOrange 配置: 假 fp-passport 服务端 + 代理转发"""
HOST = "0.0.0.0"
# 假服务端监听端口(本地)
SERVER_PORT = 8443
# 代理监听端口(客户端经此 MITM)
PROXY_PORT = 4430
# 目标域名(被代理的)
TARGET_HOST = "fp-passport.haowancheng.cn"
# 证书路径(见 cert/ 目录, 自签CA+服务器证书, 需被客户端信任)
CERT_DIR = "cert"
CERTFILE = os.path.join(CERT_DIR, "server.crt")
KEYFILE  = os.path.join(CERT_DIR, "server.key")
CAFILE   = os.path.join(CERT_DIR, "ca.crt")
'''
w('config.py', cfg)
print('config.py', len(cfg))
