# -*- coding: utf-8 -*-
"""fpxcache.fdb SQLCipher 缓存文件的离线分析与构造工具。
仅作离线分析/生成, 不随服务启动。
"""
import struct, hashlib, hmac as hmaclib, os, sys

FDB = r"F:/haowancheng/GuiLongchao/game/fpxcache.fdb"

def load():
    return open(FDB, "rb").read()

def sqlcipher_kdf_v1(passphrase, salt, iters=4000):
    """SQLCipher v1/v2/v3 KDF: PBKDF2-HMAC-SHA1。
    key     = PBKDF2(pass, salt,        iters, 32)   (AES-256)
    hmac_key= PBKDF2(pass, salt XOR 0x3a, iters, 32)
    """
    key = hashlib.pbkdf2_hmac("sha1", passphrase, salt, iters, 32)
    hsalt = bytes(b ^ 0x3A for b in salt)
    hkey = hashlib.pbkdf2_hmac("sha1", passphrase, hsalt, iters, 32)
    return key, hkey

def sqlcipher_kdf_v4(passphrase, salt, iters=256000):
    key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, iters, 32)
    hkey = hashlib.pbkdf2_hmac("sha512", passphrase, salt, 2, 32)
    return key, hkey

def verify_v1(passphrase, data, page_size=1024, iters=4000):
    """SQLCipher v1/v2/v3: 第1页无HMAC, 只验证解密后头是否 'SQLite format 3'"""
    try:
        from Crypto.Cipher import AES
    except Exception:
        return None, "no pycryptodome"
    salt = data[:16]
    key, _ = sqlcipher_kdf_v1(passphrase, salt, iters)
    body = data[16:16 + page_size]
    # SQLCipher 对第1页: 从 offset 16 起是密文(跳过 salt), 长度 = page_size - 16 (保留区)
    ct = data[16:page_size]
    if len(ct) % 16 != 0:
        ct = ct[:len(ct) - (len(ct) % 16)]
    try:
        pt = AES.new(key, AES.MODE_CBC, b"\x00" * 16).decrypt(ct)
    except Exception as e:
        return None, str(e)
    return pt[:16], pt[:48]

def verify_v4(passphrase, data, page_size=4096, iters=256000):
    """SQLCipher v4: 第1页 salt 已剥离, 明文头在解密后 +0"""
    try:
        from Crypto.Cipher import AES
        from Crypto.Protocol.KDF import PBKDF2
        from Crypto.Hash import SHA512
    except Exception:
        return None, "no pycryptodome"
    salt = data[:16]
    key = PBKDF2(passphrase, salt, 32, count=iters, hmac_hash_module=SHA512)
    ct = data[16:page_size]
    ct = ct[:len(ct) - (len(ct) % 16)]
    try:
        pt = AES.new(key, AES.MODE_CBC, b"\x00" * 16).decrypt(ct)
    except Exception as e:
        return None, str(e)
    return pt[:16], pt[:48]

if __name__ == "__main__":
    d = load()
    salt = d[:16]
    print("fpxcache.fdb  size=%d" % len(d))
    print("salt = %s" % salt.hex())
    print()
    # 尝试常见的 passphrase 候选
    cands = [
        b"", b"fpx", b"FPX", b"funplus", b"FunPlus",
        b"deep.cn.prod", b"fp_account_cn_pc", b"mubao",
        b"123456", b"fpxcache", b"fpxcache.fdb",
        b"fp_account_cn_pc.offical.Amukpj",
    ]
    print("=== 尝试 v1/v2/v3 (PBKDF2-SHA1, 4000) 明文头 ===")
    for ps in [1024, 4096]:
        for pw in cands:
            hdr, full = verify_v1(pw, d, ps, 4000)
            if hdr and hdr[:15] == b"SQLite format 3":
                print("  *** 命中! page=%d pass=%r" % (ps, pw))
                print("      %r" % full)
    print()
    print("=== 尝试 v3 (64000) ===")
    for ps in [1024, 4096]:
        for pw in cands:
            hdr, full = verify_v1(pw, d, ps, 64000)
            if hdr and hdr[:15] == b"SQLite format 3":
                print("  *** 命中! page=%d iter=64000 pass=%r" % (ps, pw))
                print("      %r" % full)
    print()
    print("=== 尝试 v4 (PBKDF2-SHA512, 256000, page=4096) ===")
    for pw in cands:
        hdr, full = verify_v4(pw, d, 4096, 256000)
        if hdr and hdr[:15] == b"SQLite format 3":
            print("  *** 命中! pass=%r" % pw)
            print("      %r" % full)
    print()
    print("(无命中属正常: passphrase 需从 FPX.dll 反汇编取得)")
