#!/usr/bin/env python3
"""剧情短信映射候选生成器（docs/计划-剧情短信RPC.md P7）。

背景：任务 -> 话题的映射数据里没有（Gate 0 结论），官方写死在服务端脚本。
我们自建 POST_SMS_BY_TASK，但**每一条都必须人工核实**——这个工具不做决定，
只把候选按可信度排好序，供人工核对后抄进 npc_sms.POST_SMS_BY_TASK。

排序依据（数据里能拿到的全部信号，不猜）：
  1. 角色一致    任务的 GuideParam/NPC 与话题 role 的 actorName 关联（弱信号）
  2. 文案重合    话题首句 vs 任务 Description/TaskBgStory 的关键词重合数
  3. 章节同段    任务 id 与话题 id 数量级接近（弱信号，仅排序用）

用法:
  python tools/sms_candidates.py                # 全 main 章节任务
  python tools/sms_candidates.py 1044           # 只看一个任务
  python tools/sms_candidates.py --min 2        # 只显示重合词数 >= 2 的候选

输出里 score 只是排序参考，**采纳与否必须人工读原文**。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import gamedata  # noqa: E402

_STOP = set("的了是在我你他她它我们你们他们这那也都很就还又再把被和跟对从向让给叫做吗呢吧啊呀哦嗯去来说对于而已".split())


def _words(text):
    """极简中文切词：2-gram + 过滤虚词。只用于排序，不用于判定。"""
    if not text:
        return set()
    t = re.sub(r"<[^>]+>", "", str(text))
    t = re.sub(r"[，。！？、：；（）\[\]{}…\s\x22\x27]", "", t)
    grams = {t[i:i + 2] for i in range(max(0, len(t) - 1))}
    return {g for g in grams if g not in _STOP and len(g) == 2}


def _topic_first_text(topic_id):
    """话题首句 + 玩家回复，全部文本拼一起（候选判定的语料）。"""
    dlg = gamedata.npc_chat_dialog(topic_id) or {}
    rows = [v for v in dlg.values() if isinstance(v, dict)]
    rows.sort(key=lambda r: int(r.get("order") or 0))
    return " ".join(str(r.get("chattext") or "") for r in rows)


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    min_hit = 2
    if "--min" in sys.argv:
        min_hit = int(sys.argv[sys.argv.index("--min") + 1])

    msg = gamedata.table("NpcMessageData") or {}
    tasks = gamedata.table("StoryTaskData") or {}
    # 只列主线章节的任务（其他章节等推到再核）
    task_ids = sorted(int(k) for k, v in tasks.items()
                      if str(v.get("ChapterId") or "").startswith("main")
                      and str(k).isdigit())
    if argv:
        task_ids = [int(t) for t in argv if t.isdigit()]

    # 每个话题的语料与 2-gram，提前算好
    topics = []
    for k, v in msg.items():
        if not isinstance(v, dict):
            continue
        try:
            tid = int(k)
        except Exception:
            continue
        text = _topic_first_text(tid)
        if not text.strip():
            continue
        topics.append((tid, v, text, _words(text)))

    for t in task_ids:
        cfg = tasks.get(str(t)) or tasks.get(t) or {}
        desc = " ".join(str(x) for x in (cfg.get("Description") or []))
        bg = str(cfg.get("TaskBgStory") or "")
        tw = _words(desc + " " + bg)
        if not tw:
            continue
        cands = []
        for tid, v, text, gws in topics:
            hit = tw & gws
            if len(hit) >= min_hit:
                cands.append((len(hit), tid, v.get("chatName"), text[:42]))
        if not cands:
            continue
        cands.sort(reverse=True)
        print("任务 %-7s %-14s %s" % (t, cfg.get("TaskName"), desc[:24]))
        for hit, tid, cname, text in cands[:4]:
            mark = "  ←已配" if npc_sms__mapped(t) == tid else ""
            print("    候选 topic=%-6s %-14s 重合=%d  首句=%s%s"
                  % (tid, cname, hit, text, mark))
        print()


def npc_sms__mapped(task_id):
    from game_server.story import npc_sms
    lst = npc_sms.topics_for_task(task_id)
    return lst[0] if lst else -1


if __name__ == "__main__":
    main()
