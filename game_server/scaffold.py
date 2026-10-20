#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.scaffold —— 脚手架覆盖层（默认关闭）。

它是什么
--------
「全系统解锁 / 任务全完成 / 等级拉高」这类为了**让 UI 在服务端还不能驱动
剧情时可用**而强行补的数据。

为什么单独抽出来
----------------
以前这些是写死在 tools/build_avatar_init.py 里、烤进 avatar_init.json 的，
生成之后就再也关不掉，而且会掩盖服务端真正的 bug。现在改成运行时覆盖：
  - avatar_init.json = 客户端声明的默认值 + 新号初始数据（合法，长期保留）
  - 本模块          = 临时脚手架，默认关闭，--unlock-all 打开

什么时候该删
------------
阶段 2/3 做剧情推进时，解锁状态要改成服务端权威：
任务完成 -> 改 system_p.unlocked_system -> 推 propertySync2Client。
那时候本模块连同 --unlock-all 一起删。

已知风险（实测过）
------------------
- AvatarUserDataComp.lua:221 "attempt to index field '?'"：
  is_char_list_shown 翻转时某个 UI 面板索引了 nil；
- 任务被标成全完成，剧情/引导判断「玩家做过 X 没有」会错乱；
- 掩盖真正的服务端 bug。

数据结构来源
------------
- 系统 id：SystemConst.AllSystemCatalogue（common/systemconst.lua:184）
- 门槛类型：AllSystemCatalogueData 的 unlockType/unlockParam
- 任务状态：GlobalConst.TaskState.Finished = 6（globalconst.lua:77）
- 自定义类型形状：generated/config.lua 里 StoryTaskItem / ActiveTask 的 Properties
"""
from . import config

# ---------------------------------------------------------------- 常量表

# 系统目录里所有的 id（私服全解锁用）
SYSTEM_UNLOCKS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    21, 22, 23, 24, 25, 28, 29, 30, 31, 32, 33, 34, 35, 42, 43, 44, 45, 46,
    47, 48, 49, 50, 51, 52, 53, 54, 55, 57, 59, 60, 61, 62, 64, 67, 68, 70,
    71, 73, 74, 75, 78, 81, 82, 1001,
]

# Task 类门槛需要这些任务「已完成」
DONE_TASK_IDS = [1, 15]
# TaskData[1] 和 TaskData[15] 的 TaskType 都是 1，所以只需要给类型 1 开槽位。
DONE_TASK_TYPES = [1]

# StoryTask 类门槛需要的剧情任务 id
DONE_STORY_TASK_IDS = [
    1005, 1007, 1010, 1031, 100003, 400101, 415802,
    415805, 415902, 416002, 416101, 416202, 430201,
]

# 覆盖后的玩家等级（Level 类门槛有 4/5/6/7）
SCAFFOLD_LEVEL = 10

# GlobalConst.TaskState.Finished
TASK_STATE_FINISHED = 6

# ---------------------------------------------------------------- 类型形状
#
# 自定义类型实例必须带 __id__，嵌套的自定义属性也要带（只是不带 __tp__）。
# 这些形状照抄 generated/config.lua 里对应类型的 Properties 默认值。

_STORY_TASK_ITEM = {
    "id": 0, "state": 0, "get_time": 0, "start_time": 0,
    "doing_targets": {}, "finish_targets": {}, "finish_parameter": {},
}

_ACTIVE_TASK = {
    "id": 0, "type": 0, "state": 0, "get_time": 0,
    "complete_count": 0, "cache": {},
}

_INT_DICT_KEY_INT = {}


# ---------------------------------------------------------------- 工具

def _max_id(obj, cur=0):
    """递归找最大的 __id__，用于分配新 id。"""
    if isinstance(obj, dict):
        v = obj.get("__id__")
        if isinstance(v, int) and v > cur:
            cur = v
        for v2 in obj.values():
            cur = _max_id(v2, cur)
    elif isinstance(obj, list):
        for v in obj:
            cur = _max_id(v, cur)
    return cur


class _IdGen(object):
    def __init__(self, start):
        self.n = start

    def next(self):
        self.n += 1
        return self.n


def _make(shape, gen):
    """按形状造一个自定义类型实例（带 __id__，嵌套的自定义属性递归处理）。"""
    out = {"__id__": gen.next()}
    for k, v in shape.items():
        out[k] = _make(v, gen) if isinstance(v, dict) else v
    return out


# ---------------------------------------------------------------- 应用

def apply(props, cfg=None):
    """把脚手架覆盖到 props 上（原地修改）。

    props 必须已经是 _conv_int_keys 之后的形态 —— 所以这里一律用**整数键**，
    不能再写 "#1"（那是 JSON 里的写法，转换之后就变成 1 了）。
    返回实际改动了哪些项，便于日志。
    """
    cfg = cfg or config.SCAFFOLD
    changed = []

    if cfg.level is not None:
        props["level"] = cfg.level
        changed.append("level=%s" % cfg.level)

    if cfg.unlock_all:
        sysp = props.get("system_p")
        if isinstance(sysp, dict):
            us = sysp.get("unlocked_system")
            if isinstance(us, dict):
                for sid in SYSTEM_UNLOCKS:
                    us[sid] = True
                changed.append("系统解锁 %d 项" % len(SYSTEM_UNLOCKS))

    if cfg.all_tasks_done:
        gen = _IdGen(_max_id(props))

        # task_bag：getSystemTaskState 会先看 task_bag[taskType] 是否存在，
        # 为空就直接返回 -1，根本走不到 done_tasks 的判断。
        tb = props.get("task_bag")
        if isinstance(tb, dict):
            for tt in DONE_TASK_TYPES:
                if tb.get(tt) is None:
                    # ★ 嵌套的自定义值**只能带 __id__，不能带 __tp__**。
                    #   类型由父容器的 __ValueType__ 决定（TaskTypeBag -> TaskBag）。
                    #   带 __tp__ 会让客户端 CustomDict 解析走错分支，
                    #   实测表现为 initProperty 提前中断 -> item_bag 没建出来 ->
                    #   AvatarBagComp:refreshLocalBagData 里 pairs(nil) 报错 ->
                    #   ClientAvatar:init 返回非布尔 -> 进游戏被踢回登录页的死循环。
                    tb[tt] = _make({}, gen)
            changed.append("task_bag 槽位 %s" % DONE_TASK_TYPES)

        # done_tasks：DoneTaskTypeBag 的 __ValueType__ 是 IntDictKeyInt
        dt = props.get("done_tasks")
        if isinstance(dt, dict):
            for tt in DONE_TASK_TYPES:
                slot = dt.get(tt)
                if not isinstance(slot, dict):
                    slot = _make(_INT_DICT_KEY_INT, gen)
                    dt[tt] = slot
                for tid in DONE_TASK_IDS:
                    slot[tid] = 1
            changed.append("done_tasks 任务 %s" % DONE_TASK_IDS)

        # done_story_tasks：StoryTaskBag 的 __ValueType__ 是 StoryTaskItem 对象，
        # 不能直接塞 true（roinit 会拿 true 去建对象然后失败）。
        dst = props.get("done_story_tasks")
        if isinstance(dst, dict):
            for tid in DONE_STORY_TASK_IDS:
                item = _make(_STORY_TASK_ITEM, gen)
                item["id"] = tid
                item["state"] = TASK_STATE_FINISHED
                dst[tid] = item
            changed.append("done_story_tasks %d 条" % len(DONE_STORY_TASK_IDS))

    return changed
