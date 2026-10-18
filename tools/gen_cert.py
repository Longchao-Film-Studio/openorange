#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools.gen_cert —— 生成 MITM 用根 CA + 服务器证书

独立运行:  python tools/gen_cert.py
输出:      tools/cert/{ca.crt,ca.key,server.crt,server.key}

安装: 把 tools/cert/ca.crt 导入 Windows "受信任的根证书颁发机构",
      客户端才会接受我们伪造的 fp-passport.haowancheng.cn 证书。
"""
import datetime
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert")

# 证书要覆盖的域名(客户端会访问的)
# 注意: 必须与 tools/proxy.py 的 TARGET_HOSTS + SERVERLIST_HOSTS 完全一致,
#       否则客户端经代理访问时 TLS 主机名校验失败。
DOMAINS = [
    # --- 认证服 (proxy.py TARGET_HOSTS) ---
    "fp-passport.haowancheng.cn",
    "fp-passport-release.nenglianghe.cn",
    "account-release.nenglianghe.cn",
    "fp-idauth-release.nenglianghe.cn",
    "privacy-release.nenglianghe.cn",
    # --- 服务器列表 (proxy.py SERVERLIST_HOSTS) ---
    # 缺失这些会让客户端拿不到 serverlist, 卡在登录界面出不去。
    "l1-prod-serverlist-deep.haowancheng.cn",
    "l2-prod-serverlist-deep.haowancheng.cn",
    "l1-dev-serverlist-deep.haowancheng.cn",
    "l2-dev-serverlist-deep.haowancheng.cn",
    # --- 本地 ---
    "localhost",
]


def _save(name, data):
    p = os.path.join(CERT_DIR, name)
    with open(p, "wb") as f:
        f.write(data)
    print("wrote", p)


def main():
    os.makedirs(CERT_DIR, exist_ok=True)
    now = datetime.datetime.utcnow()

    # ---- 根 CA ----
    # 关键: 若 CA 已存在且未加 --force-ca, 复用旧 CA。
    # 重新生成 CA 会让已导入 Windows 受信任根的旧 CA 失效, 客户端全体 TLS 失败。
    ca_key_path = os.path.join(CERT_DIR, "ca.key")
    ca_crt_path = os.path.join(CERT_DIR, "ca.crt")
    if os.path.exists(ca_key_path) and os.path.exists(ca_crt_path) and "--force-ca" not in sys.argv:
        with open(ca_key_path, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(ca_crt_path, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        ca_name = ca_cert.subject
        print("[cert] 复用已有根 CA (加 --force-ca 可强制重生成)")
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenOrange"),
            x509.NameAttribute(NameOID.COMMON_NAME, "OpenOrange Fake CA"),
        ])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        _save("ca.key", ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        _save("ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))

    # ---- 服务器证书 ----
    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName(d) for d in DOMAINS] + [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]
    srv_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenOrange"),
        x509.NameAttribute(NameOID.COMMON_NAME, DOMAINS[0]),
    ])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    _save("server.key", srv_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    _save("server.crt", srv_cert.public_bytes(serialization.Encoding.PEM))

    print("")
    print("下一步: 把 tools/cert/ca.crt 导入 Windows 受信任根证书颁发机构")
    print("        (certmgr.msc -> 受信任的根证书颁发机构 -> 证书 -> 导入)")


if __name__ == "__main__":
    main()
