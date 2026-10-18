#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth_server.server —— 认证服 (假 fp-passport)

独立启动:  python -m auth_server.server
监听:      0.0.0.0:8443 (HTTPS)

流程:
    请求体 base64 -> 解密(AES-CBC 固定IV) -> JSON -> 路由 handler
    -> JSON 响应 -> 加密([随机IV]+AES-CBC) -> base64 -> 回包
"""
import json
import os
import ssl
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import db  # noqa: E402
from auth_server import config, crypto, handlers  # noqa: E402


DEBUG_DUMP = os.environ.get("OO_DEBUG") == "1"
DUMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "work", "logs", "wire")


def _log(*a):
    print("[auth]", *a, flush=True)


def _dump(kind, path, obj):
    """把明文请求/响应落盘。默认始终开启 —— 排查期不允许丢证据。"""
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        name = path.strip("/").replace("/", "_") or "root"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rec = {"_ts": ts, "_path": path, "_kind": kind, "_body": obj}
        fn = os.path.join(DUMP_DIR, "%s.%s.jsonl" % (name, kind))
        with open(fn, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _log("dump failed:", e)


class AuthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenOrange-Auth/1.0"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_encrypted(self, obj, status=200):
        pt = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        body = crypto.b64e(crypto.encrypt_response(pt)).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code, msg, status=200):
        self._send_encrypted({"code": code, "msg": msg}, status=status)

    def _parse_request(self, raw):
        if not raw:
            return {}
        try:
            ct = crypto.b64d(raw.decode("ascii", "ignore").strip())
            pt = crypto.decrypt_request(ct)
            return json.loads(pt.decode("utf-8"))
        except Exception as e:
            _log("decrypt failed:", e)
            return None

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self._read_body()
        _log("POST", path, "body-len", len(raw),
             "auth-hdr", (self.headers.get("auth") or "")[:24])
        req = self._parse_request(raw)
        if req is None:
            _log("RAW(unparsed):", raw[:200])
            _send_raw = {"_raw": raw.decode("ascii", "ignore")[:400]}
            try:
                os.makedirs(DUMP_DIR, exist_ok=True)
                nm = path.strip("/").replace("/", "_") or "root"
                with open(os.path.join(DUMP_DIR, "%s.UNDECRYPTABLE.jsonl" % nm),
                          "a", encoding="utf-8") as f:
                    f.write(json.dumps({"_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "_body": _send_raw}, ensure_ascii=False) + "\n")
            except Exception:
                pass
            self._send_error_json(-1, "decrypt failed")
            return
        _dump("req", path, req)
        fn = handlers.ROUTES.get(path)
        if fn is None:
            _log("unhandled path:", path, "-> generic success")
            self._send_encrypted({"code": 0, "msg": "Success"})
            return
        try:
            resp = fn(req)
        except Exception:
            _log("handler error on", path)
            traceback.print_exc()
            self._send_error_json(-1, "internal error")
            return
        _dump("resp", path, resp)
        _log("->", path, "resp keys:", sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
        self._send_encrypted(resp)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            body = json.dumps({"ok": True, "service": "openorange-auth"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def serve(port=None, host=None):
    db.init()
    port = port or config.SERVER_PORT
    host = host or config.HOST
    httpd = ThreadingHTTPServer((host, port), AuthHandler)
    crt = os.path.join(config.CERT_DIR, "server.crt")
    key = os.path.join(config.CERT_DIR, "server.key")
    if os.path.exists(crt) and os.path.exists(key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        _log("HTTPS listening on %s:%d" % (host, port))
    else:
        _log("WARNING: no cert, plain HTTP on %s:%d" % (host, port))
    return httpd


def main():
    httpd = serve()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")


if __name__ == "__main__":
    main()
