"""game_server.street -- 街区业务域（P0-E2 第 4 步）。

按业务域拆分里最重的一个目录：街区切换 / 阴阳界 / NPC 交互 / 宝箱 /
声望 / 派遣 / 主线等级与 CD。

搬迁顺序（依赖从少到多）：
  enter -> surface -> interact -> treasure -> prestige -> dispatch -> misc
"""
