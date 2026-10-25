#!/usr/bin/env python3
"""phonestcore 协议黑盒探针。

原理：xlua.dll 导出了 luaopen_phonestcore，可以脱离游戏加载原生 RPC 层。
配合本地假 TCP 服务器，就能让真实的 phonestcore 客户端连上来，
从而在不进游戏的情况下观察/试探它的线协议。

用法：
    python tools/phonest_probe.py --mode capture   # 只抓客户端发什么
    python tools/phonest_probe.py --mode fuzz      # 回喂帧看客户端反应（会崩，需隔离）

已确认的事实（2026-10-19）：
  * phonestcore 需要先给 _G 装兜底元表，否则 luaopen 会 panic
    （"RpcHandlerCallbackOnConnect global function miss"）
  * 客户端 API：
        c = phonestcore.newClient(phonestcore.ConTypeTcp)
        c:startClient(ip, port, handlerType, handlerId, succId, failId)
        c:dispatchRpc(name, ...)
  * handlerType: GateClient=1, MsGateClient=3, SimpleClient=8
  * ConType: Tcp=1, Kcp=2
  * 原生回调（Lua 全局函数）：
        RpcHandlerCallbackOnConnect(cObj, mappingObj, handlerId)
        RpcHandlerCallbackOnClose(mappingObj)
        RpcHandlerCallbackOnMethod(mappingObj, funcName, ...)
        RpcHandlerCallbackProtoExceedLimit(mappingObj, size, context)
  * phonestcore.registerRpc(name) 返回 32 位 RPC 索引
  * ms 通道的握手请求帧固定为 6 字节：02 00 00 00 01 00
        [u32 LE 负载长度=2][负载=01 00]
  * 只有 sendRequestEncryptToken 会产出帧；其它 RPC 在未绑定 handler 时不发
  * 回喂错误负载会让原生解析器抛 C++ 异常（0xe06d7363），需进程隔离
"""
import argparse
import contextlib
import ctypes
import os
import socket
import sys
import threading
import time
from ctypes import POINTER, c_char_p, c_int, c_size_t, c_void_p

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STUB_LUA = r"""
CALLED = {}; COBJ = nil; MAPOBJ = nil
local function mkstub(name)
  return function(...)
    local n = select('#', ...)
    local parts = {}
    for i = 1, math.min(n, 6) do
      local v = select(i, ...)
      if type(v) == 'string' then parts[#parts+1] = string.format('%q', v)
      else parts[#parts+1] = tostring(v) end
    end
    CALLED[#CALLED+1] = name .. "(" .. table.concat(parts, ", ") .. ")"
    if name == 'RpcHandlerCallbackOnConnect' then
      COBJ = (select(1, ...)); MAPOBJ = (select(2, ...))
    end
  end
end
setmetatable(_G, {__index = function(t, k)
  local f = mkstub(tostring(k)); rawset(t, k, f); return f
end})
"""


class Phonest:
    def __init__(self, dll):
        os.add_dll_directory(os.path.dirname(os.path.abspath(dll)))
        self.lib = ctypes.CDLL(dll)
        lib = self.lib
        lib.luaL_newstate.restype = c_void_p
        for fn, args in (
            ("luaL_openlibs", [c_void_p]),
            ("luaopen_phonestcore", [c_void_p]),
            ("lua_setglobal", [c_void_p, c_char_p]),
            ("luaL_loadstring", [c_void_p, c_char_p]),
            ("lua_pcall", [c_void_p, c_int, c_int, c_int]),
        ):
            getattr(lib, fn).argtypes = args
        lib.luaL_loadstring.restype = c_int
        lib.lua_pcall.restype = c_int
        lib.luaopen_phonestcore.restype = c_int
        lib.lua_tolstring.argtypes = [c_void_p, c_int, POINTER(c_size_t)]
        lib.lua_tolstring.restype = c_char_p
        lib.lua_settop.argtypes = [c_void_p, c_int]

        self.L = lib.luaL_newstate()
        lib.luaL_openlibs(self.L)
        self.run(STUB_LUA, "stub")
        lib.luaopen_phonestcore(self.L)
        lib.lua_setglobal(self.L, b"phonestcore")

    def run(self, script, label=""):
        lib = self.lib
        lib.luaL_loadstring(self.L, script.encode("utf-8"))
        rc = lib.lua_pcall(self.L, 0, 0, 0)
        if rc != 0:
            n = c_size_t(0)
            msg = lib.lua_tolstring(self.L, -1, ctypes.byref(n))
            print("  [{}] ERR {}".format(label, msg.decode("utf-8", "replace") if msg else "?"),
                  flush=True)
            lib.lua_settop(self.L, 0)
            return False
        lib.lua_settop(self.L, 0)
        return True

    def pump(self, n=20, delay=0.02):
        for _ in range(n):
            self.run("phonestcore.pullIO()", "pull")
            time.sleep(delay)


def start_server(port, cap):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    holder = {"conn": None}

    def accept_loop():
        while True:
            c, _ = srv.accept()
            holder["conn"] = c
            threading.Thread(target=read_loop, args=(c,), daemon=True).start()

    def read_loop(c):
        c.settimeout(30)
        with contextlib.suppress(Exception):
            while True:
                d = c.recv(65536)
                if not d:
                    break
                cap.append(d)
                print(f'  [srv] RX {len(d):2d} B  {d.hex()}', flush=True)

    threading.Thread(target=accept_loop, daemon=True).start()
    return holder


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlua", required=True)
    ap.add_argument("--port", type=int, default=19992)
    ap.add_argument("--mode", choices=("capture", "fuzz"), default="capture")
    ap.add_argument("--rpc", default="sendRequestEncryptToken")
    args = ap.parse_args()

    cap = []
    holder = start_server(args.port, cap)
    ph = Phonest(args.xlua)
    ph.run("phonestcore.startIO(1)", "startIO")
    ph.run("C = phonestcore.newClient(phonestcore.ConTypeTcp)", "newClient")
    ph.run(f"C:startClient('127.0.0.1', {args.port}, phonestcore.HandlerTypeMsGateClient, 'HID', 'S', 'F')", "startClient")
    ph.pump(40)

    print(f'[probe] 已连接，抓包 {len(cap)} 个', flush=True)
    ph.run(f"pcall(function() COBJ:dispatchRpc({args.rpc!r}, 'A') end)", "dispatch")
    ph.pump(20)
    print(f'[probe] dispatch 后抓包: {[x.hex() for x in cap]}', flush=True)

    if args.mode == "fuzz":
        trials = [
            ("回显",       bytes.fromhex("020000000100")),
            ("len2+0200",  bytes.fromhex("020000000200")),
            ("len2+0000",  bytes.fromhex("020000000000")),
        ]
        for label, payload in trials:
            print(f"[probe] 发送 {label}: {payload.hex()}", flush=True)
            try:
                holder["conn"].sendall(payload)
            except Exception as e:
                print(f"  发送失败: {e}", flush=True)
            ph.pump(25)
            ph.run(r"""
local n = #CALLED
for i = math.max(1, n - 4), n do print('    回调: ' .. CALLED[i]) end
""", "cb")

    print()
    print("=== 原生回调记录 ===")
    ph.run(r"for _, v in ipairs(CALLED) do print('  ' .. v) end", "all")
    print("=== 客户端发出的帧 ===")
    for d in cap:
        print("  " + d.hex())


if __name__ == "__main__":
    main()
