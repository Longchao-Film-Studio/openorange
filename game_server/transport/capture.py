# -*- coding: utf-8 -*-
"""抓包 / 代理 / 回显模式（P0 遗留清理：从 server.py 拆出）。

这三种模式是**排查用**的，不是正式联调路径：
  python -m game_server.server                     纯抓包（什么都不回）
  python -m game_server.server --proxy host:port   反代到官方网关，观察官方应答
  python -m game_server.server --echo              收到什么原样回什么

★ 早期这里还有两个「猜协议」的分支，已删除：
    GATE_OK        —— 对每条连接都硬编码回一个 ConnectServerResp（猜类型号时期）
    RESPOND_CYCLE  —— 按连接序号轮换 6 组「帧类型 + 字段布局」应答（爆破）
  协议早已从 client_gate.proto 描述符完整解出，正式路径在 transport/gate.py。
"""
import os
import socket
import threading
import time

from .net import CAPTURE_DIR, log_packet, pump

from shared.log_tee import get_logger
_log = get_logger(__name__)


def handle_capture(conn, addr, cid, echo=False, upstream=None):
    """纯观察 / 回显 / 反代。

    echo     : 收到什么原样回什么
    upstream : (host, port)，把流量转发给上游并记录双向内容
    """
    t0 = time.time()
    cap_path = os.path.join(CAPTURE_DIR, "conn_%03d_%s.bin" % (cid, addr[1]))
    _log.info("[game] #%03d OPEN  from %s:%d" % (cid, addr[0], addr[1]))

    f = None
    try:
        f = open(cap_path, "wb")
    except Exception as e:
        _log.warning("[game] #%03d capture file error: %s" % (cid, e))

    try:
        conn.settimeout(120)

        if upstream is not None:
            up_host, up_port = upstream
            print("[game] #%03d upstream -> %s:%d" % (cid, up_host, up_port),
                  flush=True)
            up = socket.create_connection((up_host, up_port), timeout=10)
            up.settimeout(120)
            e1, e2 = threading.Event(), threading.Event()
            t1 = threading.Thread(target=pump,
                                  args=(conn, up, "C>", cid, t0, f, e1),
                                  daemon=True)
            t2 = threading.Thread(target=pump,
                                  args=(up, conn, "S<", cid, t0, f, e2),
                                  daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=300)
            t2.join(timeout=300)
            try:
                up.close()
            except Exception:
                pass
            return

        while True:
            try:
                data = conn.recv(8192)
            except socket.timeout:
                print("[game] #%03d recv timeout (idle 120s), closing" % cid,
                      flush=True)
                break
            except ConnectionResetError:
                _log.info("[game] #%03d reset by peer" % cid)
                break
            if not data:
                _log.info("[game] #%03d peer closed" % cid)
                break
            log_packet(cid, "RX", data, t0, f)
            if echo:
                try:
                    conn.sendall(data)
                except Exception as e:
                    _log.warning("[game] #%03d echo failed: %s" % (cid, e))
    except Exception as e:
        _log.warning("[game] #%03d error: %s" % (cid, e))
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        dur = int((time.time() - t0) * 1000)
        print("[game] #%03d CLOSE after %dms -> %s"
              % (cid, dur, os.path.basename(cap_path)), flush=True)
