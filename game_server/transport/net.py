"""帧收发、抓包日志、连接序号（P0 遗留清理：从 server.py 拆出）。

日志格式固定为：
  [game]/[ms]/[GATE] #<cid> <方向> +<毫秒> <字节数>
  随后是十六进制 + ASCII 转储，原始字节另存 work/captures/*.bin。
"""
import contextlib
import socket
import threading
import time

from shared.log_tee import get_logger

from .. import config as ooconf

_log = get_logger(__name__)

CAPTURE_DIR = ooconf.CAPTURE_DIR

_conn_seq = 0
_seq_lock = threading.Lock()


def next_cid():
    """给一条新连接分配 cid（日志里的 #%03d）。"""
    global _conn_seq
    with _seq_lock:
        _conn_seq += 1
        return _conn_seq



def hexdump(data, width=16, limit=None):
    """十六进制 + ASCII 转储（抓包日志用）。"""
    n = len(data) if limit is None else min(limit, len(data))
    out = []
    for off in range(0, n, width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"      {off:04X}  {hx:<{width * 3}}  |{asc}|")
    if limit is not None and len(data) > limit:
        out.append(f'      ... (共 {len(data)} 字节，其余见 .bin)')
    return "\n".join(out)


def maybe_hexdump(data, limit=None):
    """按 PACKET_LOG 开关返回 hexdump 文本；关闭时返回空串（日志降噪）。

    ★ 2026-10-23：原来每帧都 print hexdump，控制台被刷爆、排障时真实错误
      被淹没。开 OO_PACKET_LOG=1 恢复完整 hexdump。
    """
    from .. import config as ooconf
    if not ooconf.packet_log_enabled():
        return ""
    return hexdump(data, limit=limit)


def log_packet(cid, direction, data, t0, f):
    """打印一帧，并（可选）写进 .bin 抓包文件。"""
    dt = int((time.time() - t0) * 1000)
    _log.info(f'[game] #{cid:03d} {direction} +{dt:d}ms  {len(data):d} B')
    if ooconf.packet_log_enabled():
        _log.info(hexdump(data, limit=512))
    if f is not None:
        with contextlib.suppress(Exception):
            f.write((f'{direction} {dt} {len(data)}\n').encode())
            f.write(data)
            f.flush()


def pump(src, dst, direction, cid, t0, f, done_evt):
    """单向转发 + 记录（--proxy 模式用）。"""
    try:
        while True:
            try:
                data = src.recv(8192)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            log_packet(cid, direction, data, t0, f)
            try:
                dst.sendall(data)
            except OSError as e:
                _log.warning(f'[game] #{cid:03d} {direction} forward failed: {e}')
                break
    finally:
        with contextlib.suppress(Exception):
            dst.shutdown(socket.SHUT_WR)
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
