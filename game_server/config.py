#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.config —— 游戏服唯一配置源。

设计原则
--------
1. **单一真相**：端口、路径、默认值只在这里出现一次，其它模块一律 import。
   以前 33100/33101/OO00000001/avatar_2000 散落在 ms.py / server.py 里，
   改一处漏三处。

2. **脚手架是运行时可关的覆盖层**，不是烤进存档的固定值。
   以前「全解锁 / 任务全完成 / 等级拉高」是写死在 tools/build_avatar_init.py
   里、生成到 avatar_init.json 的 —— 一旦生成就再也关不掉，
   而且会掩盖服务端真正的 bug（分不清功能能用是因为服务端对了还是被强行解锁）。
   现在改成：
     avatar_init.json  = 客户端声明的默认值 + 新号初始数据（合法）
     scaffold.py       = 运行时覆盖层（全解锁等，默认关）

3. **每个开关都写清「为什么存在、什么时候该删」**。
"""
import os

# ---------------------------------------------------------------- 路径
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(_HERE)

WORK_DIR = os.path.join(ROOT_DIR, "work")
CAPTURE_DIR = os.path.join(WORK_DIR, "captures")

# 玩家存档目录。设 OO_STATE_DIR 可换（测试必须换，否则覆盖真实存档）。
STATE_DIR = os.environ.get("OO_STATE_DIR") or os.path.join(WORK_DIR, "player_state")

# 客户端声明的属性默认值（由 tools/build_avatar_init.py 生成）
AVATAR_INIT_PATH = os.path.join(_HERE, "avatar_init.json")

# ---------------------------------------------------------------- 网络
HOST = "0.0.0.0"

# 微服务网关（客户端 serverlist 里的 clientGate 指向它）
MS_PORT = 33100
# 游戏网关（客户端真正连的 connectServer 端口）
GATE_PORT = 33101
# 账号门户（web）
WEB_PORT = 8399

# 下发给客户端的地址
ADVERTISE_HOST = "127.0.0.1"
CLUSTER_ID = 20000

# ---------------------------------------------------------------- 实体
# 账号关联失败时的兜底 uid。正常路径下 uid = 登录名，不会用到它。
FALLBACK_UID = "OO00000001"

# 实体 id 前缀。实体 id = <前缀><uid>，这样多账号天然不冲突。
AVATAR_ENTITY_PREFIX = "avatar_"
ACCOUNT_ENTITY_PREFIX = "account_"

# createChannelEntity 里下发的实体类名（客户端 EntityFactory 里的注册名）
AVATAR_CLASS = "ClientAvatar"

# ---------------------------------------------------------------- 账号关联
# ms 的 GameLoadService.query 先到、gate 新连接后到，用「队列 + 时间窗」关联。
# 两个客户端**同时**启动时可能串号（先后启动没问题）。
# 要彻底解决得让客户端把账号带进 connectServer 的 auth —— 那要改客户端行为。
USERNAME_MATCH_WINDOW = 30  # 秒

# 心跳间隔约 1 秒，超过这个秒数没动静就认为掉线了。
ONLINE_STALE_SEC = 120

# ---------------------------------------------------------------- 脚手架
# ★ 脚手架 = 为了让 UI 在「服务端还不能驱动剧情」时可用而强行补的数据。
#
# 风险（实测过的）：
#   - 系统之间假设的前置数据不存在 -> AvatarUserDataComp.lua:221 报
#     "attempt to index field '?'"，因为 is_char_list_shown 翻转时某个面板索引了 nil；
#   - 剧情/引导判断「玩家做过 X 没有」会错乱（任务被标成全完成）；
#   - **会掩盖服务端真正的 bug**：分不清功能能用是因为服务端对了还是被强行解锁。
#
# 什么时候删：阶段 2/3 做剧情推进时，解锁状态要改成服务端权威
# （任务完成 -> 改 system_p.unlocked_system -> 推 propertySync2Client），
# 到时候整个 scaffold 模块连同 --unlock-all 一起删掉。
#
# ★ 切换开关后要删 work/player_state/*.json，否则老存档里的旧值不会更新
#   （脚手架只在「首次为该账号建状态」时应用）。


class Scaffold(object):
    """脚手架开关。默认全关 —— 走和真实游戏一致的开局。"""

    def __init__(self, unlock_all=False, level=None, all_tasks_done=None):
        self.unlock_all = bool(unlock_all)
        # 等级覆盖。None = 用 avatar_init.json 里的值。
        self.level = level
        # 任务全完成。None = 跟随 unlock_all。
        self.all_tasks_done = (self.unlock_all if all_tasks_done is None
                               else bool(all_tasks_done))

    def describe(self):
        if not (self.unlock_all or self.level is not None or self.all_tasks_done):
            return "关闭（走真实开局）"
        parts = []
        if self.unlock_all:
            parts.append("全系统解锁")
        if self.all_tasks_done:
            parts.append("任务全完成")
        if self.level is not None:
            parts.append("等级=%s" % self.level)
        return "开启（%s）" % "、".join(parts)


# 运行时全局脚手架配置。main() 里按命令行参数覆盖。
SCAFFOLD = Scaffold()


def configure_scaffold(unlock_all=False, level=None, all_tasks_done=None):
    global SCAFFOLD
    SCAFFOLD = Scaffold(unlock_all=unlock_all, level=level,
                        all_tasks_done=all_tasks_done)
    return SCAFFOLD


def avatar_entity_id(uid):
    return AVATAR_ENTITY_PREFIX + str(uid)


def account_entity_id(uid):
    return ACCOUNT_ENTITY_PREFIX + str(uid)
