#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game_server.stamina —— 体力（时之砂 / tower_reward_keys）。

数据依据
--------
属性：
  tower_reward_keys            当前体力
  next_key_recover_timestamp   下一点恢复的**绝对时间戳**（满了 = 0）
  total_key_recover_timestamp  恢复满的**绝对时间戳**（满了 = 0）

客户端读取（ui/panel/streetplayoverviewpanel/node_rogue_info/node_rogue_infogroup.lua:114-132）：
  local isMax = GlobalData.Avatar.next_key_recover_timestamp == 0
  recoverTime = GlobalData.Avatar.total_key_recover_timestamp - G.timemgr:NowI()
  -- 显示「xx:xx:xx 后恢复满」

上限（同文件 102-112）：
  maxNum = CommonConstData.KeyNumMax + LevelExpData[level].maxStaminaAdd
           + (月卡 VIP ? CommonConstData.ExtraKeyNumMaxVip : 0)

恢复速度（CommonConstData）：
  KeyRefresh = 360   # 每 360 秒恢复 1 点
  KeyNumMax  = 180
  ExtraKeyNumMaxVip = 60

为什么需要这个模块
------------------
以前 initDict 里 tower_reward_keys = 0、两个时间戳都是 0，结果：
  1. 开局体力是 0 不是满的；
  2. 体力不会随时间恢复（服务端从来没算过）；
  3. 使用时砂之盒也不加（UseItem 只扣数量，没应用 useParam）。
"""
import time

from . import gamedata

# 属性名
PROP_KEYS = "tower_reward_keys"
PROP_NEXT = "next_key_recover_timestamp"
PROP_TOTAL = "total_key_recover_timestamp"


def _int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def max_stamina(level=1, is_vip=False):
    """体力上限 = KeyNumMax + 等级加成 + 月卡加成。"""
    base = _int(gamedata.const("KeyNumMax", 180), 180)
    le = gamedata.level_exp(level) or {}
    add = _int(le.get("maxStaminaAdd"), 0)
    if is_vip:
        add += _int(gamedata.const("ExtraKeyNumMaxVip", 60), 60)
    return base + add


def refresh_sec():
    """每多少秒恢复 1 点。"""
    return max(1, _int(gamedata.const("KeyRefresh", 360), 360))


def is_full(props, level=1, is_vip=False):
    return _int(props.get(PROP_KEYS)) >= max_stamina(level, is_vip)


def init_new(props, level=1, is_vip=False):
    """新号：体力拉满，两个时间戳清零（= 已满）。"""
    props[PROP_KEYS] = max_stamina(level, is_vip)
    props[PROP_NEXT] = 0
    props[PROP_TOTAL] = 0
    return props[PROP_KEYS]


def apply_recovery(props, level=1, is_vip=False, now=None):
    """按离线时间补体力，返回本次补了几点。

    规则：从 next_key_recover_timestamp 起，每 KeyRefresh 秒 +1，补到上限为止。
    补满后两个时间戳都归 0（客户端据此显示「已满」、不显示倒计时）。
    """
    now = int(now if now is not None else time.time())
    mx = max_stamina(level, is_vip)
    keys = _int(props.get(PROP_KEYS))
    if keys >= mx:
        changed = (keys != mx) or _int(props.get(PROP_NEXT)) != 0 or _int(props.get(PROP_TOTAL)) != 0
        props[PROP_KEYS] = mx
        props[PROP_NEXT] = 0
        props[PROP_TOTAL] = 0
        return 0, changed

    step = refresh_sec()
    nxt = _int(props.get(PROP_NEXT))
    if nxt <= 0:
        # 时间戳丢了（老存档），从现在开始计时
        nxt = now + step
        props[PROP_NEXT] = nxt
        props[PROP_TOTAL] = nxt + (mx - keys - 1) * step
        return 0, True

    gained = 0
    if now >= nxt:
        gained = min((now - nxt) // step + 1, mx - keys)
        keys += gained
        nxt += gained * step

    props[PROP_KEYS] = keys
    if keys >= mx:
        props[PROP_NEXT] = 0
        props[PROP_TOTAL] = 0
    else:
        props[PROP_NEXT] = nxt
        props[PROP_TOTAL] = nxt + (mx - keys - 1) * step
    return gained, True


def add(props, amount, level=1, is_vip=False, now=None):
    """加体力（道具效果），超过上限的部分丢弃（真实游戏会转邮件，暂未实现）。

    返回 (实际加了几个, 是否被截断)。
    """
    now = int(now if now is not None else time.time())
    mx = max_stamina(level, is_vip)
    cur = _int(props.get(PROP_KEYS))
    want = _int(amount)
    if want <= 0:
        return 0, False
    newv = cur + want
    truncated = newv > mx
    newv = min(newv, mx)
    props[PROP_KEYS] = newv
    if newv >= mx:
        props[PROP_NEXT] = 0
        props[PROP_TOTAL] = 0
    else:
        step = refresh_sec()
        nxt = _int(props.get(PROP_NEXT))
        if nxt <= 0:
            nxt = now + step
        props[PROP_NEXT] = nxt
        props[PROP_TOTAL] = nxt + (mx - newv - 1) * step
    return newv - cur, truncated