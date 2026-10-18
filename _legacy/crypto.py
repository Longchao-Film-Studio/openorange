#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# fp-passport 请求/响应 加密模块(已破解, 2026-09-09 运行时抓取确认)
# 算法: AES-128-CBC, PKCS7 padding
# Key = b'1wTOoUwJ4FxhIsoU'  (从 FPX.dll 运行时 EVP_AES init rcx 抓到, 固定 app key)
# IV  = b'1234567890abcdef'  (由明文块0数学推得)
# 明文 = JSON, 例 send_code: {"app_id":"deep.cn.prod","device_id":"...","lang":"zh-cn","from":"sdk","os":"win","bind_type":101,"csc":"86","code_type":1,"mobile":"18888888888"}
import base64
KEY  = b'1wTOoUwJ4FxhIsoU'
IV   = b'1234567890abcdef'
MODE = "CBC"

def _pad(b):
    n = 16 - (len(b) % 16); return b + bytes([n])*n
def _unpad(b):
    return b[:-b[-1]]
def encrypt_plaintext(pt: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    e = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return e.update(_pad(pt)) + e.finalize()
def decrypt_ciphertext(ct: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    d = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
    return _unpad(d.update(ct) + d.finalize())
def b64e(b): return base64.b64encode(b).decode()
def b64d(s): return base64.b64decode(s)

def encrypt_response(pt: bytes) -> bytes:
    # fp-passport 响应格式 = [16B 随机IV][AES-CBC(key, IV, pt)]。客户端从响应头16B读IV。
    import os
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    iv = os.urandom(16)
    e = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    return iv + e.update(_pad(pt)) + e.finalize()
def decrypt_request(ct: bytes) -> bytes:
    # 客户端请求 = AES-CBC(key, 固定IV) 纯密文(无前缀)。用请求固定IV解。
    return decrypt_ciphertext(ct)