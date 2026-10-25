#!/usr/bin/env python3
"""start_server.py —— 一键启动 OpenOrange 的本地服务（开发用）

用法:
    python start_server.py              # 后端(8399) + 游戏服(33100/33101)
    python start_server.py --backend    # 只启后端
    python start_server.py --game       # 只启游戏服
    python start_server.py --no-window  # 在当前窗口前台运行（Ctrl+C 停止）

每个服务各开一个独立控制台窗口，方便看日志，也方便单独关闭。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

SERVICES = {
    "backend": {
        "title": "OO-Backend:8399",
        "args": ["-u", "-m", "web.app", "--port", "8399"],
        "ports": [8399],
    },
    "game": {
        "title": "OO-GameServer:33100+33101",
        # --unknown-ok：兼容模式。未实现的 RPC/微服务方法回"成功"，
        # 避免客户端 2 秒弱网转圈/重连风暴（仍然逐条记到
        # work/logs/unknown_requests.tsv，用来排缺口优先级）。
        # 不带这个参数 = 严格模式（回失败），适合审计跑。
        "args": ["-u", "-m", "game_server.server", "--ms-ok", "--unknown-ok"],
        "ports": [33100, 33101],
    },
}


def start(svc, new_window=True):
    cfg = SERVICES[svc]
    print("[{}] {}".format(svc, " ".join(cfg["args"])))
    cmd = [sys.executable] + cfg["args"]
    kwargs = {"cwd": ROOT}
    if new_window:
        # 新窗口：标题 + 不自动关闭
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        full = "title {} && {}".format(
            cfg["title"],
            subprocess.list2cmdline(cmd),
        )
        subprocess.Popen(["cmd.exe", "/k", full], **kwargs)
    else:
        subprocess.call(cmd, **kwargs)


def check_ports():
    """用 TCP 连接探测端口是否在监听（不依赖外部命令）。"""
    import socket

    print()
    print("=== 端口检查 ===")
    allports = []
    for cfg in SERVICES.values():
        allports += cfg["ports"]
    ok = True
    for p in sorted(allports):
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", p))
            print(f'  {p:<6d} 监听中')
        except Exception:
            print(f'  {p:<6d} 未监听（看新开的窗口里有没有报错）')
            ok = False
        finally:
            s.close()
    return ok


def main():
    argv = sys.argv[1:]
    only_backend = "--backend" in argv
    only_game = "--game" in argv
    no_window = "--no-window" in argv

    if not only_backend and not only_game:
        only_backend = only_game = True

    print(f"root: {ROOT}")
    print(f"python: {sys.executable}")
    print()

    if only_backend:
        start("backend", not no_window)
        if not no_window:
            time.sleep(1.0)

    if only_game:
        start("game", not no_window)

    if no_window:
        print()
        print("前台模式：按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("已停止")
        return 0

    time.sleep(4)
    check_ports()

    print()
    print("接下来启动游戏（再开一个窗口）:")
    print("    cd {}\\launcher\\dist".format(ROOT.replace("/", "\\")))
    print("    .\\OO_Launcher.exe --cli <账号> <密码>")
    print()
    print("注意: 启动器那个窗口不要关，关掉会连带关掉游戏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
