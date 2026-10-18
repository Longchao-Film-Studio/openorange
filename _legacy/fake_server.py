#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 假 fp-passport 服务端: 处理 /api/send_code 与 /api/login。
# 用 Python 标准库 http.server + ssl 实现 HTTPS。
import json, base64, ssl, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, crypto

# ---- 会话/验证码状态(内存) ----
_codes = {}          # phone -> code
_logged = {}         # phone -> account info

def _resp_error(msg="fail", code=-1):
    # 官方响应是加密体; 这里构造"明文"结构(真实schema待FPX_CRYPTO_FINDINGS), 由crypto加密
    return {"code": code, "msg": msg}

class FPH(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _read_body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(ln) if ln else b""
    def do_POST(self):
        path = self.path
        raw = self._read_body()
        print("[OpenOrange] POST", path, "body-len", len(raw), flush=True)
        if path.endswith("/api/send_code"):
            self._handle_send_code(raw)
        elif path.endswith("/api/login"):
            self._handle_login(raw)
        else:
            self._resp_bytes(self._wrap_error(404, "no such api"), 404)
    def _handle_send_code(self, raw):
        phone = None
        # 若key已提取, 解密请求得到手机号; 否则从请求密文无法读, 先打日志
        try:
            ct = crypto.b64d(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            pt = crypto.decrypt_request(ct)
            print("[OpenOrange] send_code plaintext:", pt[:120], flush=True)
            phone = self._guess_phone(pt)
        except Exception as e:
            print("[OpenOrange] send_code decrypt n/a:", e, flush=True)
        # 生成验证码(固定123456, 便于测试)并存
        code = "123456"
        if phone: _codes[phone] = code
        print("[OpenOrange] send_code issued code=", code, "for", phone, flush=True)
        # 返回"发送成功"明文结构(待按真实schema加密)
        plain = {"code": 0, "msg": "ok"}
        self._resp_bytes(self._enc(plain))
    def _handle_login(self, raw):
        phone = code = None
        try:
            ct = crypto.b64d(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            pt = crypto.decrypt_request(ct)
            print("[OpenOrange] login plaintext:", pt[:160], flush=True)
            phone, code = self._guess_phone_code(pt)
        except Exception as e:
            print("[OpenOrange] login decrypt n/a:", e, flush=True)
        # 校验: code==123456 即成功(假服务端)
        if code == "123456":
            acct = _login_success(phone)
            print("[OpenOrange] LOGIN SUCCESS", acct, flush=True)
            plain = acct   # {code:1, data:{account_id,ticket,source}}
        else:
            print("[OpenOrange] login bad code", code, flush=True)
            plain = {"code": 1, "msg": "bad code"}
        self._resp_bytes(self._enc(plain))
    def _enc(self, plain):
        # response=[16B random IV][AES-CBC(key,IV,json)]
        return crypto.encrypt_response(json.dumps(plain, separators=(",", ":")).encode())

    def _wrap_error(self, c, m):
        return json.dumps({"code": c, "msg": m}).encode()
    def _resp_bytes(self, b, status=200):
        b64 = crypto.b64e(b)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b64)))
        self.end_headers()
        self.wfile.write(b64.encode())
    def _guess_phone(self, pt):
        try:
            o=json.loads(pt.decode())
            return str(o.get('mobile') or o.get('phone') or '')
        except Exception:
            return None
    def _guess_phone_code(self, pt):
        try:
            o=json.loads(pt.decode())
            return str(o.get('mobile') or o.get('phone') or ''), str(o.get('code') or '')
        except Exception:
            return None, None

def _login_success(phone):
    # fp-passport 登录成功响应 = LoginAccountRet 平铺结构(静态 .rdata 反射表确证字段)。
    # 注: code:1 + data 是 SDK 上层(C# FPXSDKBridge->Lua onSdkLogin)的回调格式,
    # 不是 fp-passport 原始响应。假服在 fp-passport 层, 应返回 LoginAccountRet 平铺。
    # 字段: fp_uid/session_key/is_new/fp_device_id/msg/account_create_ts/channel_uid/
    #        account_id/fpid_list/current_fpid/ticket/delete_time_left/is_formal
    import time
    phone = phone or "test001"
    return {
        "fp_uid": "fp_" + phone,
        "session_key": "openorange_session_" + phone,
        "is_new": True,
        "fp_device_id": "",
        "msg": "Success",
        "account_create_ts": int(time.time()),
        "channel_uid": phone,
        "account_id": "oo_" + phone,
        "fpid_list": [],
        "current_fpid": None,
        "ticket": "openorange_ticket_" + phone,
        "delete_time_left": 0,
        "is_formal": True,
    }

def serve(port=None):
    port = port or config.SERVER_PORT
    httpd = HTTPServer((config.HOST, port), FPH)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cf = os.path.join(config.CERT_DIR, "server.crt"); kf = os.path.join(config.CERT_DIR, "server.key")
    if os.path.exists(cf) and os.path.exists(kf):
        ctx.load_cert_chain(cf, kf)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print("[OpenOrange] fake server HTTPS on", port, flush=True)
    else:
        print("[OpenOrange] fake server HTTP(no cert) on", port, flush=True)
    httpd.serve_forever()

if __name__ == "__main__":
    serve()