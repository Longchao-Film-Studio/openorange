#!/usr/bin/env python3
"""剧情任务「追踪目标」的唯一判定入口（R3：一个概念只允许一条实现）。

── 为什么要收敛（2026-10-25 实机事故）────────────────────────────────

之前 `street_trace_taskid` 有 **三个写入点、三套不同规则**：

| 位置 | 触发时机 | 当时的规则 |
|---|---|---|
| story/rpc.py `_advance_trace_after_finish` | 任务完成 | 仅当「追踪的正是刚完成的任务」才顺移，否则什么都不做 |
| street/enter.py `_refresh_trace_on_street_change` | 切街区 | 用 `_oo_trace_wanted` 重算，找不到就**随便挑一个**本街区正在做的任务 |
| street/interact.py `_street_trace_task` | 玩家手动追踪 | 只判 `GuideSceneId`，**不看任务是否已完成** |

实机症状（用户复现）：奶茶店主线打完后 NPC 带着继续推，走到「进龙潮阁」那一步
剧情死活触发不了；左上角左上显示的却是**已经做完的** 1044「接到杜老爷消息」；
退出重登后立刻就能触发。

根因：三套规则叠加，追踪标点可以停在已完成任务上、也可能被换成无关任务
（实机日志 `[street] 追踪目标 1044 不在街区 100，切到 400009`）。玩家按错误标点
找，自然找不到下一步；只有重登（重算一次）才恢复。

── 不变量（INV）──────────────────────────────────────────────────────

  服务端主动刷新（auto）写下的 `street_trace_taskid` 要么是 0，要么满足：
    1) 该任务在 story_task_bag 里 state == Doing（3）
    2) GuideSceneId 为空，或 == 当前 street_id

  玩家手动追踪（manual）写下的则必须**恰好等于玩家点名的那一个**（在本街区可显示时），
  否则为 0 —— 绝不回退到别的任务。

  这两条由结构保证：任何地方要改追踪只能调 apply()，写不出
  「指向已完成任务」「换街区时挑无关任务」这两类错误。测试见 tests/test_trace_invariant.py。
"""

from .. import gamedata
from .. import ms as msmod
from ..rpc_common import SYNC_SELF, sync_frame

# story.state_machine.TASK_STATE_DOING（本地定义避免包初始化期循环导入）
TASK_STATE_DOING = 3

WANTED_KEY = "_oo_trace_wanted"   # 玩家手动追踪意图（跨街区保留）
TRACE_KEY = "street_trace_taskid"  # 真正下发给客户端的追踪任务


def _as_int(v, default=None):
    """宽容转 int：存档 JSON 里的键可能是字符串。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except Exception:
        return default


def _bag_items(st):
    """story_task_bag 归一化成 {int task_id: item}。"""
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        return {}
    out = {}
    for k, v in bag.items():
        if not isinstance(v, dict):
            continue
        tid = _as_int(k)
        if tid is None or tid <= 0:
            continue
        out[tid] = v
    return out


def _is_doing(item):
    return _as_int(item.get("state"), -1) == TASK_STATE_DOING


def _done_ids(st):
    done = st.props.get("done_story_tasks")
    if not isinstance(done, dict):
        return set()
    out = set()
    for k in done:
        tid = _as_int(k)
        if tid is not None and tid > 0:
            out.add(tid)
    return out


def _gs(task_id):
    """任务的 GuideSceneId（int），没有则 None。"""
    gs = (gamedata.story_task(task_id) or {}).get("GuideSceneId")
    if gs is None:
        return None
    return _as_int(gs)


def _scene_ok(task_id, cur_street):
    """任务的 GuideSceneId 是否允许在当前街区显示标点。

    没有 GuideSceneId 的（type0/6/7 大多没有）视为「不限街区」，与客户端
    node_task_pos.lua 的判断一致。
    """
    gs = _gs(task_id)
    return True if gs is None else gs == cur_street


def _is_stale_wanted(st, task_id):
    """玩家意图是否已失效：任务已完成、或配置里根本不存在。

    ★ 只清这两种。**不能**因为「任务还没开始 / 在别的街区」就清——
      玩家追踪别的街区的任务时，意图必须留着，走到该街区才能恢复标点
      （tests/test_story_trace.py [9] 就锁这个行为）。
    """
    if task_id <= 0:
        return False
    if task_id in _done_ids(st):
        return True
    return not bool(gamedata.story_task(task_id))


def _chapter(task_id):
    return (gamedata.story_task(task_id) or {}).get("ChapterId")


def _street_of(st, street_id):
    if street_id is not None:
        return _as_int(street_id, 0) or 0
    return _as_int(st.props.get("street_id"), 0) or 0


def resolve(st, street_id=None, mode="auto", manual_task=None):
    """唯一判定：当前应该追踪哪个任务。返回 id 或 0（不追踪）。

    mode="manual"（玩家手动点了追踪）:
        只认 manual_task：在本街区能显示就返回它，否则 0。不回退到别的任务。
    mode="auto"（任务完成 / 切街区等服务端刷新）:
        候选 = 正在做 + 本街区；优先玩家意图（若在候选里）> 同章节 > 主线 > id 升序；
        一个都没有 -> 0（绝不挑无关任务）。
    """
    sid = _street_of(st, street_id)
    if mode == "manual":
        tid = _as_int(manual_task, 0) or 0
        if tid <= 0:
            return 0
        return tid if _scene_ok(tid, sid) else 0

    items = _bag_items(st)
    if not items:
        return 0

    # (a) 玩家手动意图优先：未失效 + 本街区可显示就恢复它。
    #     ★ 这里**不要求**任务正在做 —— 玩家点名要追踪的就是它，
    #       客户端也允许追踪未开始的任务（实机行为，tests [9d]）。
    wanted = _as_int(st.props.get(WANTED_KEY), 0) or 0
    if wanted > 0 and not _is_stale_wanted(st, wanted) and _scene_ok(wanted, sid):
        return wanted

    # (b) 自动候选：先「明确指定本街区」的，再「不限街区」的。
    #     分两层是为了不让 gs=None 的任务抢掉真正属于本街区的任务
    #     （tests [8]：切到街区 100 后必须给出 GuideSceneId=100 的任务）。
    doing = {t: it for t, it in items.items() if _is_doing(it)}
    tier_explicit = [t for t in doing if _gs(t) == sid]
    tier_any = [t for t in doing if _gs(t) is None]
    for cands in (tier_explicit, tier_any):
        if not cands:
            continue
        if wanted in cands:
            return wanted
        ref_chapter = _chapter(wanted) if wanted > 0 else None

        def _rank(t, _ref=ref_chapter):
            ch = _chapter(t)
            same_chapter = 0 if (_ref and ch == _ref) else 1
            is_main = 0 if str(ch or "").startswith("main") else 1
            return (same_chapter, is_main, t)

        return sorted(cands, key=_rank)[0]
    return 0


def apply(st, syncs=None, reason="", street_id=None, force_zero=False,
          mode="auto", manual_task=None):
    """写追踪目标 + 推同步帧（唯一入口）。返回写入的 id。

    ★ 无论值变不变都推一次 OP_CHANGE：客户端标点刷新只绑这个属性的 OP_CHANGE
      事件（streettasktrackcomp.lua），值相同不推就不会重画。
    """
    # 1) 归一化玩家意图（只清「已完成 / 不存在」，理由见 _is_stale_wanted）
    if force_zero:
        st.props[WANTED_KEY] = 0
    else:
        w = _as_int(st.props.get(WANTED_KEY), 0) or 0
        if _is_stale_wanted(st, w):
            st.props[WANTED_KEY] = 0

    # 2) 判定 + 落库
    old = _as_int(st.props.get(TRACE_KEY), 0) or 0
    tid = 0 if force_zero else resolve(st, street_id=street_id, mode=mode,
                                       manual_task=manual_task)
    st.props[TRACE_KEY] = tid

    # 3) 推帧（永远推）
    if syncs is not None:
        syncs.append(sync_frame(SYNC_SELF, 0, msmod.OP_CHANGE, TRACE_KEY, tid))

    if old != tid or reason:
        print("[trace] {}：{} -> {}（街区 {}，{}）".format(reason or "刷新", old, tid, _street_of(st, street_id), mode),
              flush=True)
    return tid


def note_wanted(st, task_id):
    """记录玩家「想追踪什么」（可以是别的街区的任务：走到那里自动恢复标点）。

    负数（客户端取消追踪的哨兵 -1000）与非法值一律归 0。
    """
    tid = _as_int(task_id, 0) or 0
    if tid < 0:
        tid = 0
    st.props[WANTED_KEY] = tid
    return tid
