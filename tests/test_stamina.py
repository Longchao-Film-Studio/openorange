#!/usr/bin/env python
"""体力（时之砂 / tower_reward_keys）测试。

数据依据：
  CommonConstData.KeyNumMax = 180 / KeyRefresh = 360 / ExtraKeyNumMaxVip = 60
  LevelExpData[level].maxStaminaAdd（lv1=0 / lv10=40 / lv60=60）
  客户端读取：node_rogue_infogroup.lua:114-132（next=0 表示已满）

修复的三个问题：
  1. 用道具不加体力（UseItem 只扣数量，没应用 useParam）
  2. 开局体力不是满的（initDict 里是 0）
  3. 体力不随时间恢复（服务端从来没算过）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OO_STATE_DIR"] = tempfile.mkdtemp(prefix="oo_test_stamina_")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import config
from game_server import gamedata as G
from game_server import ms as msmod
from game_server import player_state as pstate
from game_server import stamina as S
from tests.frame_guard import guarded_handle  # noqa: E402

passed, failed = [], []

def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))

def run():
    print("\n[1] 上限与恢复速度（读 BDD）")
    check("KeyNumMax = 180", G.const("KeyNumMax") == 180, G.const("KeyNumMax"))
    check("KeyRefresh = 360", G.const("KeyRefresh") == 360, G.const("KeyRefresh"))
    check("lv1 上限 = 180", S.max_stamina(1) == 180, S.max_stamina(1))
    check("lv10 上限 = 220", S.max_stamina(10) == 220, S.max_stamina(10))
    check("lv10+月卡 = 280", S.max_stamina(10, True) == 280, S.max_stamina(10, True))

    print("\n[2] 新号体力拉满")
    p = {}
    S.init_new(p, 10)
    check("体力 = 220", p[S.PROP_KEYS] == 220, p[S.PROP_KEYS])
    check("next = 0（已满）", p[S.PROP_NEXT] == 0, p[S.PROP_NEXT])
    check("total = 0（已满）", p[S.PROP_TOTAL] == 0, p[S.PROP_TOTAL])

    print("\n[3] 随时间恢复（每 360 秒 1 点）")
    now = 1000000
    p2 = {S.PROP_KEYS: 0, S.PROP_NEXT: 0, S.PROP_TOTAL: 0}
    g, _ = S.apply_recovery(p2, 10, now=now)
    check("首次只设时间戳，不立即给点", g == 0, g)
    check("next = now + 360", p2[S.PROP_NEXT] == now + 360, p2[S.PROP_NEXT])
    g, _ = S.apply_recovery(p2, 10, now=now + 359)
    check("359 秒还没到点", g == 0, g)
    g, _ = S.apply_recovery(p2, 10, now=now + 360)
    check("360 秒 +1 点", g == 1 and p2[S.PROP_KEYS] == 1, (g, p2[S.PROP_KEYS]))
    g, _ = S.apply_recovery(p2, 10, now=now + 360 + 720)
    check("再等 720 秒 +2 点", g == 2 and p2[S.PROP_KEYS] == 3, (g, p2[S.PROP_KEYS]))
    g, _ = S.apply_recovery(p2, 10, now=now + 360 * 1000)
    check("离线很久直接补满", p2[S.PROP_KEYS] == 220, p2[S.PROP_KEYS])
    check("补满后 next = 0", p2[S.PROP_NEXT] == 0, p2[S.PROP_NEXT])

    print("\n[4] 加体力（道具）")
    p3 = {S.PROP_KEYS: 100, S.PROP_NEXT: 0, S.PROP_TOTAL: 0}
    added, trunc = S.add(p3, 60, 10, now=now)
    check("100 + 60 = 160", p3[S.PROP_KEYS] == 160, p3[S.PROP_KEYS])
    check("没截断", trunc is False)
    check("next 被设上（未满）", p3[S.PROP_NEXT] == now + 360, p3[S.PROP_NEXT])
    added, trunc = S.add(p3, 1000, 10, now=now)
    check("超上限被截断到 220", p3[S.PROP_KEYS] == 220, p3[S.PROP_KEYS])
    check("报告截断", trunc is True)
    check("满了 next 归零", p3[S.PROP_NEXT] == 0, p3[S.PROP_NEXT])

    print("\n[5] 时砂之盒(110) 的效果数据")
    it = G.item(110)
    check("useType = avatarAttr", (it or {}).get("useType") == "avatarAttr",
          (it or {}).get("useType"))
    check("useParam.TowerRewardKeys = 60",
          G.item_use_param(110).get("TowerRewardKeys") == 60,
          G.item_use_param(110))

    print("\n[6] UseItem_CS 端到端（扣数量 + 加体力 + 推同步）")
    config.configure_scaffold(unlock_all=True)
    msmod._live_init_dict("stamina_test")
    st = pstate.get_state(uid="stamina_test")
    st.props[S.PROP_KEYS] = 100
    bag = st.props["item_bag"]
    bag["9"] = {"__id__": 99001, "id": "9", "itemId": 110, "num": 3,
                 "ext": {"__id__": 99002}}
    frames = guarded_handle(msmod.RPC_INDEX_USE_ITEM_CS, msmod.mp_arr([110, 1, None]), 5,
                         uid="stamina_test")
    check("返回多帧（应答 + 道具同步 + 体力同步）", len(frames) >= 3, len(frames))
    check("体力 100 -> 160", st.props[S.PROP_KEYS] == 160, st.props[S.PROP_KEYS])
    check("道具 3 -> 2", bag["9"]["num"] == 2, bag["9"]["num"])

    print("\n[7] 背包里没有该道具时不动数据")
    before = st.props[S.PROP_KEYS]
    guarded_handle(msmod.RPC_INDEX_USE_ITEM_CS, msmod.mp_arr([99999, 1, None]), 6,
                uid="stamina_test")
    check("体力没变", st.props[S.PROP_KEYS] == before, st.props[S.PROP_KEYS])

    print("\n[8] 数量不足时不动数据")
    before = st.props[S.PROP_KEYS]
    guarded_handle(msmod.RPC_INDEX_USE_ITEM_CS, msmod.mp_arr([110, 99, None]), 7,
                uid="stamina_test")
    check("体力没变", st.props[S.PROP_KEYS] == before, st.props[S.PROP_KEYS])
    check("道具数量没变", bag["9"]["num"] == 2, bag["9"]["num"])

    print("\n" + "=" * 56)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(run())