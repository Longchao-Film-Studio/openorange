#!/usr/bin/env python3
"""剧情任务的 RPC 入口与落状态（P0-E2 第 2 步）。

落状态的**唯一**入口是 finish_story_tasks：
  - story.events / story.check_chain_tasks 只做「检测」，返回命中的 taskId；
  - 这里才真正调 story.finish_story_task（写 story_task_bag / done_story_tasks）、
    发奖、发 GetItem 事件、重算系统解锁位。
踩过的坑：如果在检测处直接落状态，外层再调这里会因为「已经是 Finished」
而跳过，导致不发奖、不推同步帧。
"""

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 剧情任务推进（阶段 2 核心）
# ---------------------------------------------------------------------------
# 逆向依据 entities/avatarcomponent/avatarstorytaskcomp.lua:
#   getStoryTaskState(taskID)  读 story_task_bag[taskID].state / done_story_tasks
#   getTaskIdByParamInt        遍历 story_task_bag 找 TaskType+Parameter1 命中的
#   RequestDoneStoryTaskParamInt_CS {taskId, param_int}
#   RequestDoneStoryTaskParamStr_CS {taskId, param_str}
#   RequestStreetActor_CS {actor_id}  = RequestDoneStoryTaskParamInt_CS(2, actor_id)
#     + serverRpc("RequestStreetActor_CS")  ← 街景 NPC 交互就是剧情推进

import contextlib
import time

from shared.log_tee import get_logger

from .. import gamedata, story
from .. import ms as msmod
from ..rpc_common import (
    EVENT_MAX_DEPTH,
    SYNC_SELF,
    decode_params,
    respond_after_sync,
    state,
    sync_frame,
)
from . import events as _events
from . import npc_sms

_log = get_logger(__name__)

def _story_sync_frames(st, changes):
    """把 story 模块的同步描述转成 propertySync 帧。"""
    frames = []
    for ch in changes:
        kind = ch[0]
        if kind == "root":
            _, name, value = ch
            frames.append(sync_frame(
                SYNC_SELF, 0, msmod.OP_CHANGE, name, value))
        elif kind == "add":
            # ★ 给自定义 dict **加新键**必须用 OP_ADD（客户端 __OpAdd__），
            #   用 OP_CHANGE 会走 __OpChange__，那里 oldVal 为 nil 直接报
            #   "subproperty is nil" 并把这次变更丢掉。
            #   实测症状：剧情任务完成后 story_task_bag 删掉了，
            #   但 done_story_tasks 里没有 -> 客户端 getTaskStateByTaskID 返回 -1。
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_ADD, str(key), value))
        elif kind == "sub":
            _, container, key, value = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_CHANGE, str(key), value))
        elif kind == "child":
            # 改「某个条目内部的子属性」（例如 story_task_bag.<id>.finish_parameter）：
            # propertyId 是**条目自己的 __id__**，name 是子属性名。
            # 客户端 config.lua:9941-9944 的 changed["story_task_bag.*.finish_parameter"]
            # 就是靠这个路径命中的。
            _, obj, name, value = ch
            if not isinstance(obj, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, obj.get("__id__") or 0, msmod.OP_CHANGE, str(name), value))
        elif kind == "del":
            _, container, key, _v = ch
            c = st.props.get(container)
            if not isinstance(c, dict):
                continue
            frames.append(sync_frame(
                SYNC_SELF, c.get("__id__") or 0, msmod.OP_DEL, str(key), None))
    return frames

def _advance_trace_after_finish(st, finished, frames):
    """任务完成后刷新追踪目标 —— 统一走 story.trace.apply（唯一入口 R3）。

    历史（2026-10-22 实机反馈 Bug D）：任务完成后 `street_trace_taskid` 仍指向
    那个已完成的任务，地图标点停在旧地方。当时的修法是「只在 cur == 刚完成的任务」
    时才顺移——但那样一来，追踪一旦被别的路径写成过期值就永远自愈不了。

    2026-10-25 实机事故（追踪停在已完成的 1044，导致龙潮阁剧情触发不了）就是
    这个「不自愈」的直接后果。现在统一由 story.trace 重算，天然自愈。
    """
    if not finished:
        return
    from .trace import apply as _trace_apply
    _trace_apply(st, frames, "任务完成")
def _unlock_data_satisfied(st, row):
    """这个系统入口的**数据条件**是否满足。

    和客户端 `ui/panel/streetv2panel/node_menu_item/node_menu_itemgroup.lua:231-256`
    的 `checkSystemUnlockState` 前半段一一对应：

      unlockType == 1 (Task)      -> getSystemTaskState(unlockParam) == Finished
      unlockType == 2 (StoryTask) -> getTaskStateByTaskID(unlockParam) == Finished
      unlockType == 3 (Level)     -> level >= unlockParam
      其它取值                     -> 客户端 `local unlock = true`（不参与判定）

    返回 True/False。
    """
    ut = row.get("unlockType")
    up = row.get("unlockParam")
    if ut == 1:
        from .. import tasks as _tasks
        if not isinstance(up, int):
            return False
        return _tasks.task_state(st.props, up) == _tasks.TASK_STATE_FINISHED
    if ut == 2:
        if not isinstance(up, int):
            return False
        return story.story_state(st.props, up) == story.TASK_STATE_FINISHED
    if ut == 3:
        try:
            need = int(up or 0)
        except Exception:
            return False
        try:
            lv = int(st.props.get("level") or 0)
        except Exception:
            lv = 0
        return lv >= need
    # 未知 unlockType：客户端默认 unlock=true，所以数据条件视为满足
    return True


def recalc_system_unlocks(st):
    """按 AllSystemCatalogueData 重算 system_p.unlocked_system（三类 unlockType 全覆盖）。

    ★ 这是系统解锁的**唯一写入口**。客户端判定在
      `ui/panel/streetv2panel/node_menu_item/node_menu_itemgroup.lua:231-256`：

        unlock = <数据条件>  and  isSysEntryUnlock(sysId)
        isSysEntryUnlock -> system_p.unlocked_system[sysId] == true

      也就是说**两样都得维护**：数据条件由客户端本地算，`unlocked_system` 只能服务端推。

    ★ 2026-10-22 修复前的状态（roadmap 队列第 2 项）：
      本函数只处理 `unlockType == 2`，另外两类（1 通用任务 40 条 / 3 等级 21 条）
      从来不解锁 —— 61/83 个系统入口永远没有按钮。
      见 docs/功能解锁research.md。

    ★ 双向维护：数据条件不满足时会把 `unlocked_system[sid]` 摘掉（防误解锁）。
      正常玩法里条件只会「变真」（升级/完成任务），所以反向极少触发；
      一旦触发会打日志，便于发现异常。

    ★ 客户端对 `system_p.unlocked_system` 只声明了 changed/entryAdded，没有
      entryDeleted（generated/config.lua:10328-10360），所以「摘掉」这条同步
      客户端收不到 —— 服务端状态是对的，但要下次登录才可见。已在日志里标注。
    """
    frames = []
    sp = st.props.get("system_p")
    if not isinstance(sp, dict):
        return frames
    us = sp.get("unlocked_system")
    if not isinstance(us, dict):
        return frames
    us_id = us.get("__id__") or 0
    added = []
    removed = []
    for row in gamedata.all_system_catalogue():
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        if not isinstance(sid, int):
            continue
        want = _unlock_data_satisfied(st, row)
        cur = us.get(sid) is True
        if want == cur:
            continue
        if want:
            us[sid] = True
            added.append(sid)
        else:
            us.pop(sid, None)
            removed.append(sid)
    if added or removed:
        st.set("system_p", sp)
        for sid in added:
            frames.append(sync_frame(
                SYNC_SELF, us_id, msmod.OP_CHANGE, str(sid), True))
        if added:
            _log.info(f"[story] 解锁系统 {added}")
        if removed:
            # 客户端没有 entryDeleted 回调，只能下次登录生效
            _log.info(f"[story] ★ 摘除系统解锁 {removed}（客户端需重登才可见）")
    return frames


# 兼容旧名字（dispatch / 测试里用过 _recalc_story_unlocks）
_recalc_story_unlocks = recalc_system_unlocks


def _emit_get_item(st, item_id, count):
    """发一条 GetItem 事件（覆盖 4 条剧情任务）。

    ★ 审计 P0-2：不能依赖客户端 RecordItemObtain_CS ——
      客户端只在 ItemData.firstObtainNotice 为真且首次获取时才发，
      全表只有 6 个道具有这个标记，而剧情任务要的 701/118 都没有。
      所以服务端在真正发道具的地方主动发事件。

    参数尽量补全：TaskEventData 里 GetItem 的 KeyList 有
      [itemId] / [itemType,level,grade] / [itemType,grade] /
      [itemType,itemSubType] / [itemType] 五种，
    能查到 ItemData 就把 itemType/grade/level/itemSubType 都带上。
    """
    if not item_id:
        return []
    item_type = grade = level = sub_type = None
    with contextlib.suppress(Exception):
        info = gamedata.item(item_id) or {}
        for name, box in (("itemType", "t"), ("grade", "g"),
                          ("level", "l"), ("itemSubType", "s")):
            v = info.get(name)
            if isinstance(v, int):
                if box == "t":
                    item_type = v
                elif box == "g":
                    grade = v
                elif box == "l":
                    level = v
                else:
                    sub_type = v
    ev = _events.ev_get_item(int(item_id), int(count or 1), item_type, level,
                             grade)
    if sub_type is not None:
        ev.params["itemSubType"] = sub_type
    try:
        res = _events.apply_event(st, ev)
        return list(res.hits) + list(res.chain_hits)
    except Exception as e:
        _log.warning(f"[story] GetItem 事件失败 {item_id}: {e}")
        return []

# ★ P1 阶段 N（审计 P0-1 修复）：完成剧情任务的「来源 -> 允许的 TaskType」白名单。
#
#   为什么必须有：审计实测 `Street_Actor_InteractID_Update_CS` 的第 4 个参数
#   被当成 taskId 直接喂给 finish_story_tasks，可以在街区里完成任意在包任务
#   （含 3 条 type0 根）—— 因为那条路径绕过了 _done_story_task 的 TaskType 白名单。
#
#   修在唯一入口而不是各调用点（R3）：每个来源只能完成它语义上该完成的任务类型。
#   没列出的来源 = None = 不限制（服务端内部路径：发奖联动、GM、种子）。
SOURCE_ALLOWED_TYPES = {
    "client": story.TASK_TYPE_CLIENT,          # 1/2/5 客户端主动上报
    "street_interact": (2,),                  # NPC 交互只能完成 type2（type4 已改为「切街到达」）
    "street_arrival": (4,),                     # ★ 切街到达目标街 -> type4（唯一正确路径）
    "space_task": (0,),                         # 战斗内 SpaceTask 只能完成 type0
    "event": story.TASK_TYPE_EVENT,               # 3/7 事件判定（type4 不再走这里）
    "chain": story.TASK_TYPE_CHAIN,             # 6 链式闸门
    "internal": None,                           # 不限制
}


def drain_event_task_sync(st, res):
    """转发到 story.tasks.drain_event_task_sync。

    ★ 为什么是转发而不是模块级 import：story/tasks.py 在模块级
      `from .rpc import _story_sync_frames`，rpc 反向模块级导入会成环。
      局部导入是安全的（模块已缓存，开销可忽略）。
    """
    from .tasks import drain_event_task_sync as _d
    return _d(st, res)


def finish_story_tasks(st, task_ids, depth=0, param=None, source="internal"):
    """完成若干剧情任务，返回全部同步帧。

    ★ P0-D：完成之后会发一条 FinishTask 事件进事件总线。
      depth 是递归保护：任务图理论上无环，但外部数据不一定可靠。

    ★ 审计 P1-4：save 放进 finally —— 之前只在 depth==0 的**正常返回**路径
      调 save，递归里抛异常就完全不落盘，内存改了但盘上没改。
    """
    # ★ 来源白名单：先把不该由这个来源完成的任务剔掉（R3：修在唯一入口）
    allowed = SOURCE_ALLOWED_TYPES.get(source)
    if allowed is not None:
        keep = []
        for _tid in task_ids:
            try:
                _tid = int(_tid)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
            _tt = (gamedata.story_task(_tid) or {}).get("TaskType")
            if _tt in allowed:
                keep.append(_tid)
            else:
                _log.warning(f"[story] 拒绝完成：任务 {_tid} 的 TaskType={_tt} 不允许由「{source}」完成")
        task_ids = keep
        if not task_ids:
            return []
    frames = []
    now = int(time.time())
    finished_now = []
    try:
        return _finish_story_tasks_inner(st, task_ids, depth, frames, now,
                                         finished_now, param)
    finally:
        if depth == 0:
            try:
                st.save()
            except Exception as e:
                _log.warning(f"[story] 保存失败: {e}")

def _finish_story_tasks_inner(st, task_ids, depth, frames, now, finished_now,  # noqa: C901,PLR0912  — 剧情任务完成的唯一落状态入口：发奖/归档/推进/防伪造四段必须在同一临界区
                              param=None):
    done_bag = st.props.get("done_story_tasks")
    if not isinstance(done_bag, dict):
        done_bag = {}
    if not isinstance(st.props.get("story_task_bag"), dict):
        # 没有 story_task_bag 就什么都做不了（也别发奖）
        return frames
    for tid in task_ids:
        # ★ 审计 P0-2：判「是否真完成」要看**有没有归档进 done**，不能只看 state==6。
        #   否则「标记完成 -> 发奖失败」留下的 bag-state-6 任务会被永远跳过，
        #   既不会重试发奖，也不会归档。
        if isinstance(done_bag.get(int(tid)), dict):
            continue
        # ★ 能不能完成由唯一入口判定（在包里 / 多目标集齐 / 防伪造）
        if not _ready_to_finish(st, tid, param, frames):
            continue
        # ★ P1 阶段 A：完成拆成三步，发奖插在中间
        #   1) →Finished(6)：一条 OP_CHANGE，客户端靠它触发场记板/章节提示/清红点
        #   2) 发奖：失败时任务仍在包里且状态 Finished，可重试（不会静默丢奖励）
        #   3) 归档 + 推进 PostTasks
        # ★ 升级遗留窗口（审计 P1-新，已知并接受）：2bbc5c4 那版会留下
        #   「bag 里 state=6、已经发过奖、但没归档」的存档，那种存档没有发奖账本，
        #   新代码重试时会再发一遍奖。**无法可靠区分**「发奖失败」和「升级遗留」
        #   （两者都是 bag-state-6 + 无账本），所以这里只告警、不猜：
        #   - 若是发奖失败 -> 重试补发，正确；
        #   - 若是升级遗留 -> 会多领一次，已知风险（分支从未发布，只影响本地测试存档）。
        if (story.story_state(st.props, tid) == story.TASK_STATE_FINISHED
                and story.has_award_items(tid)
                and not story.already_awarded(st, tid)):
            _log.warning(f"[story] 注意：任务 {tid} 是 bag-state-6 且无发奖账本，"
                  "按「发奖失败重试」处理（若是升级遗留存档会多领一次）")
        frames.extend(_story_sync_frames(
            st, story.mark_story_task_finished(st, tid, now)))
        # 发奖励（新格子用 OP_ADD，已有格子用 OP_CHANGE）
        for award in story.story_award(st, tid):
            _item_id, _cnt, grid_key, is_new = award
            # ★ P0-D：服务端发 GetItem 事件（审计 P0-2：
            #   客户端只在 6 个 firstObtainNotice 道具首次获取时才发
            #   RecordItemObtain_CS，任务要的 701/118 永远等不到 RPC，
            #   所以必须服务端自己发）
            _get_hits = _emit_get_item(st, _item_id, _cnt)
            if _get_hits and depth < EVENT_MAX_DEPTH:
                frames.extend(finish_story_tasks(st, _get_hits, depth + 1))
            bag = st.props.get("item_bag")
            if not isinstance(bag, dict):
                break
            grid = bag.get(grid_key)
            if isinstance(grid, dict):
                frames.append(sync_frame(
                    SYNC_SELF, bag.get("__id__") or 0,
                    msmod.OP_ADD if is_new else msmod.OP_CHANGE,
                    str(grid_key), grid))
        frames.extend(_story_sync_frames(
            st, story.archive_story_task(st, tid, now)))
        # ★ P6 短信钩子：本任务刚归档，它的 PostTasks 已由 archive_story_task
        #   启动（进入 Doing）。若映射表里有短信，此刻解锁——
        #   与「接任务」的 OP_ADD+OP_CHANGE 同批下发，客户端一次收到。
        #   失败只告警（npc_sms 内部已兜），绝不影响任务推进。
        for _nxt in (gamedata.story_task(tid) or {}).get("PostTasks") or []:
            try:
                frames.extend(npc_sms.unlock_for_task(
                    st, int(_nxt), source="posttask"))
            except Exception as _e:
                _log.warning("[sms] PostTasks %s 短信解锁异常: %s", _nxt, _e)
        _log.info(f"[story] 完成剧情任务 {tid}")
        finished_now.append(tid)
    # ★ Bug D：把追踪从已完成的任务上挪走（否则地图标点停在旧地方）
    _advance_trace_after_finish(st, finished_now, frames)
    # ★ P0-D：把「完成了哪些任务」作为事件发出去。
    #   审计 P1-2 实测：116 条 FinishTask 事件里**没有任何一条**被
    #   TaskType 3/7 的剧情任务引用，所以这段对剧情任务 0 命中；
    #   真正起作用的是下面 _recalc_story_unlocks + 外层 events.tick 的链式检查。
    #   保留它是为了将来数据里出现 FinishTask 引用时能自动生效。
    if finished_now and depth < EVENT_MAX_DEPTH:
        for tid in finished_now:
            try:
                res = _events.apply_event(
                    st, _events.ev_finish_task(tid, source="internal"))
                frames.extend(drain_event_task_sync(st, res))
                nxt = list(res.hits) + list(res.chain_hits)
                if nxt:
                    frames.extend(finish_story_tasks(st, nxt, depth + 1))
            except Exception as e:
                _log.warning(f"[story] FinishTask 事件失败 {tid}: {e}")
    elif finished_now:
        _log.info(f'[story] 事件链超过 {EVENT_MAX_DEPTH} 层，停止递归（任务 {finished_now[:5]}）')
    frames.extend(_recalc_story_unlocks(st))
    # ★ 阶段 C：增量播种。只扫「PreTask 指向刚完成的任务」的那些，不是全表重扫
    #   （全表重扫是阶段 D 的事）。depth==0 才做，避免递归里反复播。
    if depth == 0:
        from . import seed as _seed
        try:
            _extra = []
            for _tid in finished_now:
                _extra.extend(_seed.seed(st, now, only_children_of=int(_tid)))
            if _extra:
                frames.extend(_story_sync_frames(st, _extra))
        except Exception as e:
            _log.warning(f"[story] 增量播种失败: {e}")
    return frames


def _ready_to_finish(st, tid, param, frames):
    """判定任务是否**真的可以完成**；顺便把本次 param 记进 finish_parameter。

    ★ 这个判定必须放在 finish_story_tasks（唯一落状态入口）里，不能放在各个 RPC 调用点。
      审计实测：真实 NPC 路径走 street/interact.py:_actor_interact，它以前直接
      finish_story_tasks(st,[task_id]) 不经过累积 —— 4 条 TaskType 2 的多目标任务照样「第一个就完成」。
      修在调用点 = 补丁（R3）；修在入口 = 模型。

    规则：
      0. **TaskType 6（链式闸门）直接放行** —— 它的 Parameter1 是「前置 story 任务 id 列表」，
         完成条件已由 check_chain_tasks 的 all(...) 判完（story/__init__.py:469）。
         这里若再按「多目标集齐」判一次，链式任务会被自己的判定挡住，永远卡 Doing。
         （审计 P1-A 实测：4000/5000/417607 三条全被误伤，其中 417607 可达。）
      1. 任务**必须已在 story_task_bag 里**（否则拒绝，防伪造完成 Type0/Type4）。
      2. Parameter1 只有 1 项：直接可以完成。
      3. Parameter1 多项（且非 TaskType 6）：param 非空时先记 finish_parameter；
         只有集齐全部才允许完成；param 为空且没集齐 -> 拒绝（审计 P0-3）。
    """
    tid = int(tid)
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict) or not isinstance(bag.get(tid), dict):
        _log.info(f"[story] 忽略：任务 {tid} 不在 story_task_bag 里")
        return False
    cfg = gamedata.story_task(tid) or {}
    if cfg.get("TaskType") == 6:
        return True
    p1 = cfg.get("Parameter1") or []
    if not isinstance(p1, list) or len(p1) <= 1:
        return True
    if param is not None:
        frames.extend(_story_sync_frames(
            st, story.mark_finish_parameter(st, tid, param)))
    item = (st.props.get("story_task_bag") or {}).get(tid)
    got = story.finish_parameters_of(item)
    missing = [int(x) for x in p1 if int(x) not in got]
    if missing:
        _log.info(f"[story] 任务 {tid} 多目标未集齐，还差 {missing}")
        return False
    return True

def _done_story_task(rid, params, uid=None):
    """RequestDoneStoryTaskParamInt_CS / ParamStr_CS —— 参数第 0 项是 taskId。"""
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, [])
    try:
        tid = int(vals[0])
    except Exception:
        return respond_after_sync(rid, [])
    st = state(uid)
    # ★ 审计 P1-既有：不校验 taskId 就能完成任意任务（伪造包可以完成 Type0/Type4
    #   并领奖）。这里要求：任务必须在 story_task_bag 里，且 TaskType 属于客户端
    #   会主动上报的那三种（1 剧情演出 / 2 NPC 交互 / 5 视频）。
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict) or not isinstance(bag.get(tid), dict):
        _log.info(f"[story] 忽略完成请求：任务 {tid} 不在 story_task_bag 里")
        return respond_after_sync(rid, [])
    ttype = (gamedata.story_task(tid) or {}).get("TaskType")
    if ttype not in story.TASK_TYPE_CLIENT:
        _log.warning(f"[story] 忽略完成请求：任务 {tid} 的 TaskType={ttype} 不是客户端可上报类型")
        return respond_after_sync(rid, [])
    param = None
    if len(vals) > 1:
        try:
            param = int(vals[1])
        except Exception:
            param = None
    syncs = finish_story_tasks(st, [tid], param=param, source="client") or []
    return respond_after_sync(rid, syncs)

def _finish_space_task(rid, params, uid=None):
    """FinishSpaceTask_Directly_CS {spaceTaskId} —— 战斗内 SpaceTask 完成上报。

    ★ 这是 TaskType=0 的**唯一**完成路径（P1 阶段 J）。

      客户端证据链：
        battlecontainer/base/player.lua:416-425
          function Player:finishSpaceTask(taskId)
            local spaceTaskData = self:_getTaskInfo(taskId)
            if spaceTaskData and spaceTaskData.avatarTask then
              self:startSyncAvatarTask(taskId)      -- 有剧情任务就同步
            else
              GlobalData.Soul:sendServerRpc("FinishSpaceTask_Directly_CS", {taskId})
            end
          end
        :451  startSyncAvatarTask 最终也是发同一条 RPC（带 5 次重试）

      服务端要做的：按 SpaceTaskData[spaceTaskId].avatarTask 找到剧情任务，
      走**同一条** finish_story_tasks 落状态（不另拼帧）。

    ★ 防刷：只接受「avatarTask 指向的剧情任务确实在包里」的请求；
      不在包里就忽略（和 _done_story_task 的规则一致）。
    """
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, [])
    try:
        sid = int(vals[0])
    except Exception:
        return respond_after_sync(rid, [])
    tid = story.space_task_avatar_task(sid)
    if tid is None:
        # 没有 avatarTask 的 SpaceTask 只影响战斗内 UI，不改剧情状态
        _log.info(f"[story] SpaceTask {sid} 没有 avatarTask，忽略")
        return respond_after_sync(rid, [])
    st = state(uid)
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict) or not isinstance(bag.get(tid), dict):
        _log.warning(f"[story] 忽略 SpaceTask {sid} 的完成请求：剧情任务 {tid} 不在包里")
        return respond_after_sync(rid, [])
    # ★ 防伪造：必须真的在那个副本的那个关卡里。
    #   SpaceTaskData 每行都有 dungeonId + levelId，而 StartGame 会把当前
    #   副本/关卡记进 _oo_cur_copy / _oo_cur_level（dungeon/context.py）。
    #   不校验的话，伪造包可以坐在街区里发 FinishSpaceTask_Directly_CS 完成任意
    #   type0 任务（270 条），等于跳过全部副本内容。
    row = gamedata.space_task(sid) or {}
    want_copy = row.get("dungeonId")
    want_level = row.get("levelId")
    cur_copy = st.props.get("_oo_cur_copy")
    cur_level = st.props.get("_oo_cur_level")
    # ★ 注意必须「要求相等」而不是「不等才拒」：
    #   cur_copy 为 None（玩家不在任何副本里）时也要拒 ——
    #   否则「不在副本」反而绕过校验，这正是伪造包会利用的状态。
    if want_copy and str(cur_copy) != str(want_copy):
        _log.warning(f"[story] 拒绝 SpaceTask {sid}：当前副本 {cur_copy}，要求 {want_copy}")
        return respond_after_sync(rid, [])
    if want_level and str(cur_level) != str(want_level):
        _log.warning(f"[story] 拒绝 SpaceTask {sid}：当前关卡 {cur_level}，要求 {want_level}")
        return respond_after_sync(rid, [])
    _log.info(f"[story] SpaceTask {sid} 完成 -> 剧情任务 {tid}")
    syncs = finish_story_tasks(st, [tid], source="space_task") or []
    return respond_after_sync(rid, syncs)


def _street_actor(rid, params, uid=None):
    """RequestStreetActor_CS {actor_id} —— 街景 NPC 交互。

    客户端 avatarstreetcomp.lua:339-342 先调
      RequestDoneStoryTaskParamInt_CS(2, actor_id)
    再调 serverRpc("RequestStreetActor_CS", {actor_id})。
    这里按同样的规则找命中任务：story_task_bag 里 TaskType==2 且 Parameter1 含 actor_id。
    """
    vals = decode_params(params)
    if not (isinstance(vals, list) and vals):
        return respond_after_sync(rid, [])
    try:
        actor_id = int(vals[0])
    except Exception:
        return respond_after_sync(rid, [])
    st = state(uid)
    hits = _find_story_tasks_by_param(st, actor_id, 2)
    if hits:
        syncs = finish_story_tasks(st, hits, param=actor_id,
                                    source="street_interact") or []
        return respond_after_sync(rid, syncs)
    return respond_after_sync(rid, [])

def _find_story_tasks_by_param(st, param_int, task_type):
    """照 getTaskIdByParamInt 的规则找任务。"""
    out = []
    bag = st.props.get("story_task_bag")
    if not isinstance(bag, dict):
        return out
    for tid, item in bag.items():
        if not isinstance(item, dict):
            continue
        cfg = gamedata.story_task(tid) or {}
        if cfg.get("TaskType") != task_type:
            continue
        p1 = cfg.get("Parameter1") or []
        if param_int in p1:
            out.append(int(tid))
    return out
