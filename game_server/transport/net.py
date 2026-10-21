# -*- coding: utf-8 -*-
"""帧收发、抓包日志、连接序号（P0 遗留清理：从 server.py 拆出）。

日志格式固定为：
  [game]/[ms]/[GATE] #<cid> <方向> +<毫秒> <字节数>
  随后是十六进制 + ASCII 转储，原始字节另存 work/captures/*.bin。
"""
import socket
import threading
import time

from .. import config as ooconf

CAPTURE_DIR = ooconf.CAPTURE_DIR

_conn_seq = 0
_seq_lock = threading.Lock()

_ms_seq = 0
_ms_lock = threading.Lock()


def next_cid():
    """给一条新连接分配 cid（日志里的 #%03d）。"""
    global _conn_seq
    with _seq_lock:
        _conn_seq += 1
        return _conn_seq


def next_ms_seq():
    """ms 层的 seqId 序号（和 gate 的 cid 分开计）。"""
    global _ms_seq
    with _ms_lock:
        _ms_seq += 1
        return _ms_seq


def hexdump(data, width=16, limit=None):
    """十六进制 + ASCII 转储（抓包日志用）。"""
    n = len(data) if limit is None else min(limit, len(data))
    out = []
    for off in range(0, n, width):
        chunk = data[off:off + width]
        hx = " ".join("%02X" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("      %04X  %-*s  |%s|" % (off, width * 3, hx, asc))
    if limit is not None and len(data) > limit:
        out.append("      ... (共 %d 字节，其余见 .bin)" % len(data))
    return "\n".join(out)


def log_packet(cid, direction, data, t0, f):
    """打印一帧，并（可选）写进 .bin 抓包文件。"""
    dt = int((time.time() - t0) * 1000)
    print("[game] #%03d %s +%dms  %d B" % (cid, direction, dt, len(data)), flush=True)
    print(hexdump(data, limit=512), flush=True)
    if f is not None:
        try:
            f.write(("%s %d %d\n" % (direction, dt, len(data))).encode())
            f.write(data)
            f.flush()
        except Exception:
            pass


def pump(src, dst, direction, cid, t0, f, done_evt):
    """单向转发 + 记录（--proxy 模式用）。"""
    try:
        while True:
            try:
                data = src.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            log_packet(cid, direction, data, t0, f)
            try:
                dst.sendall(data)
            except OSError as e:
                print("[game] #%03d %s forward failed: %s" % (cid, direction, e), flush=True)
                break
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        done_evt.set()


def recv_exact(conn, n):
    """读满 n 字节；对端提前关闭返回 None。"""
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf
