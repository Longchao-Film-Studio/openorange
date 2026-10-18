#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth_server.config —— 认证服配置"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 监听: 固定 0.0.0.0 (全工程约定)
HOST = "0.0.0.0"

# 认证服 HTTPS 端口(客户端经代理/ hosts 打到这)
SERVER_PORT = 8443

# 客户端访问的域名(用于生成 get_config)
PUBLIC_HOST = "fp-passport.haowancheng.cn"

# 证书目录(与 tools/gen_cert.py 共用)
CERT_DIR = os.path.join(BASE_DIR, "tools", "cert")

# 验证码有效期(秒)
CODE_TTL = 300
# 票据有效期(秒)
TICKET_TTL = 86400

# 环境标识(对应 sec-Tag: deep.cn.prod-v1)
ENV = "deep.cn.prod"
APP_ID = "deep.cn.prod"
