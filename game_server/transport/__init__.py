"""game_server.transport —— 传输层（P0 遗留清理：从 server.py 拆出）。

server.py 原来 923 行，混了四件事：
  1. 帧收发与抓包日志（net.py）
  2. 多账号 uid 关联（accounts.py）
  3. 游戏网关连接处理（gate.py）
  4. 微服务连接处理（ms_service.py）
  5. 早期协议探测期的抓包/代理/回显模式（capture.py）

现在 server.py 只负责「解析命令行 + 起监听 + 分发」，每个文件都 < 400 行。
"""
