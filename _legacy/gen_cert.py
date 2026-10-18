#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 生成自签 CA + localhost 服务器证书(供假服务端 HTTPS 用)。
# 需把 ca.crt 装进客户端/系统信任才能让客户端不报证书错(或接受 pinning 策略)。
import os, datetime, ipaddress
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

base = os.path.dirname(os.path.abspath(__file__))
certd = os.path.join(base, "cert")
os.makedirs(certd, exist_ok=True)

def _save(name, data):
    with open(os.path.join(certd, name), "wb") as f:
        f.write(data)
    print("wrote", name)

# --- CA ---
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OpenOrange Fake CA")])
ca = (x509.CertificateBuilder()
      .subject_name(ca_name).issuer_name(ca_name)
      .public_key(ca_key.public_key())
      .serial_number(x509.random_serial_number())
      .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
      .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
      .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
      .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, key_encipherment=True, content_commitment=True, data_encipherment=False, key_agreement=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
      .sign(ca_key, hashes.SHA256()))
_save("ca.crt", ca.public_bytes(serialization.Encoding.PEM))
_save("ca.key", ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))

# --- server cert (SAN: localhost/127.0.0.1 + 目标域名) ---
srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
sans = [x509.DNSName("localhost"), x509.DNSName("127.0.0.1"),
        x509.DNSName("fp-passport.haowancheng.cn"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
srv = (x509.CertificateBuilder()
       .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
       .issuer_name(ca.subject)
       .public_key(srv_key.public_key())
       .serial_number(x509.random_serial_number())
       .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
       .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=825))
       .add_extension(x509.SubjectAlternativeName(sans), critical=False)
       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
       .sign(ca_key, hashes.SHA256()))
_save("server.crt", srv.public_bytes(serialization.Encoding.PEM))
_save("server.key", srv_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
print("done. Trust ca.crt in client for MITM.")
