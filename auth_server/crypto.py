#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth_server.crypto —— fp-passport 应用层加密(AES-128-CBC / PKCS7)

已确证(来源: fpx_config.json + 真实请求解密):
    api_key = "1wTOoUwJ4FxhIsoU"      (16B ASCII)
    api_iv  = "1234567890abcdef"      (16B ASCII, 固定)

请求(客户端->服务器): base64( AES-CBC(key, 固定IV, 明文JSON) )
响应(服务器->客户端): base64( [16B随机IV] + AES-CBC(key, 随机IV, 明文JSON) )
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

KEY = b"1wTOoUwJ4FxhIsoU"
IV = b"1234567890abcdef"


def _pad(data: bytes) -> bytes:
    p = padding.PKCS7(128).padder()
    return p.update(data) + p.finalize()


def _unpad(data: bytes) -> bytes:
    u = padding.PKCS7(128).unpadder()
    return u.update(data) + u.finalize()


def encrypt_plaintext(plaintext: bytes, iv: bytes = None) -> bytes:
    """明文 -> 密文(无前缀)。客户端请求用固定 IV。"""
    iv = iv or IV
    enc = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    return enc.update(_pad(plaintext)) + enc.finalize()


def decrypt_ciphertext(ciphertext: bytes, iv: bytes = None) -> bytes:
    """密文(无前缀) -> 明文。"""
    iv = iv or IV
    dec = Cipher(algorithms.AES(KEY), modes.CBC(iv)).decryptor()
    return _unpad(dec.update(ciphertext) + dec.finalize())


def encrypt_request(plaintext: bytes) -> bytes:
    """按客户端请求格式加密(固定 IV)。"""
    return encrypt_plaintext(plaintext, IV)


def decrypt_request(ciphertext: bytes) -> bytes:
    """解密客户端请求(固定 IV)。"""
    return decrypt_ciphertext(ciphertext, IV)


def decrypt_response(raw: bytes) -> bytes:
    """解密'服务器响应'格式(前 16B 随机 IV)。用于自测。"""
    return decrypt_ciphertext(raw[16:], raw[:16])


def encrypt_response(plaintext: bytes) -> bytes:
    """构造服务器响应: [16B 随机 IV] + AES-CBC(明文)。"""
    iv = os.urandom(16)
    return iv + encrypt_plaintext(plaintext, iv)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text) -> bytes:
    if isinstance(text, str):
        text = text.encode("ascii")
    return base64.b64decode(text)


if __name__ == "__main__":
    pt = b'{"mobile":"18888888888","code":"123456"}'
    ct = encrypt_request(pt)
    assert decrypt_request(ct) == pt
    resp = encrypt_response(pt)
    assert decrypt_response(resp) == pt
    print("crypto self-test OK")
