"""看某个账号的等级状态（实机测试 H3 用）。

用法: python tools/show_level.py [uid]      # 默认 bagcheck
"""
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from game_server import level  # noqa: E402

uid = sys.argv[1] if len(sys.argv) > 1 else "bagcheck"
path = os.path.join(ROOT, "work", "player_state", f"{uid}.json")
try:
    st = json.load(open(path, encoding="utf-8"))
except OSError:
    print("找不到存档:", path)
    raise SystemExit(1) from None

p = st.get("props") or {}
lv = p.get("level") or 1
exp = p.get("exp") or 0
need = level.exp_need(lv)
la = p.get("levelup_awards") or {}
ints = {k: v for k, v in la.items() if isinstance(k, int)}
claimed = sorted(k for k, v in ints.items() if v == 1)
pending = sorted(k for k, v in ints.items() if v == 0 and level.award_of(k))

print("账号            :", uid)
print("等级            :", lv, "/", level.max_level())
print("本级经验        :", exp, "/", need, "（够升级）" if need and exp >= need else "（不够）")
print("累计投入升级    :", p.get("all_exp") or 0)
print("已领奖励的等级  :", claimed or "（还没领过）")
print("可领未领的等级  :", pending[:10] or "（没有）")
