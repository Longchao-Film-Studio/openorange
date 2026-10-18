#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# OpenOrange 配置
import os
HOST = "0.0.0.0"
SERVER_PORT = 8443   # 假服务端
PROXY_PORT = 4430    # MITM 代理
TARGET_HOST = "fp-passport.haowancheng.cn"
_D = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(_D, "cert")
CERTFILE = os.path.join(CERT_DIR, "server.crt")
KEYFILE  = os.path.join(CERT_DIR, "server.key")
CAFILE   = os.path.join(CERT_DIR, "ca.crt")
