#!/usr/bin/env python3
"""ClientAvatar 的 initDict（登录快照）与 createChannelEntity（P0-E3 拆出）。

三个要点：
  1. AVATAR_INIT_DICT 来自 avatar_init.json（property/spec 的序列化缓存），
     登录时整包下发给客户端，客户端 initProperty 会逐项 assert。
  2. _live_init_dict 是「登录那一刻的状态快照」：先跑一次 _login_tick（事件总线
     主动推进），再按根属性剥掉 _oo_* 私有键和嵌套 __tp__。
     ★ 嵌套 __tp__ 漏出去会让客户端 roinit 把字符串当子项 -> 崩（审计 P0-1）。
  3. 新号的剧情起点（StoryTask 1000 设为 Doing）也在这里落，不在脚手架里 ——
     街景 NPC 的 preTask 命中 Doing(3) 才会出现。
"""
import json
import os

from .defaults import _DEF_AVATAR_EID, _DEF_UID, ooconf
from .frames import frame
from .protocol import _conv_int_keys, mp_map, pb_bytes_field

from shared.log_tee import get_logger
_log = get_logger(__name__)

#
# ChannelEntityInfo 的字段 1 是 **string**，不是 uint32。
#
# 依据（实测，2026-10-19）：
#   Lua 侧 GateClientRpcHandler:createChannelEntity(entityType, initDict, entityId)
#   把 entityType 直接传给 EntityFactory.createEntity(name, entityId) —— 收的是类名字符串。
#   把字段 1 按 varint 发（哪怕值正好是 genRpcIndex("ClientAvatar")=1292148699），
#   客户端日志是：
#       [GateClientRpcHandler] createChannelEntity (, avatar_2000) failed
#       [EntityFactory] createEntity failed: can not find entity class
#   第一个参数是**空字符串** —— 说明字段 1 被当成 string 解，varint 解不出来。
#
#   另外：RpcIndex 表确实能反查（registerRpc 之后 getRpcName(1292148699) 返回
#   "ClientAvatar"），但客户端并不用它来还原这个名字，所以补注册实体类名是
#   无害但也不是必需的。真正关键的是字段类型。
#
# 字段布局（推断）：
#   string type = 1;  bytes info = 2;  string entityId = 3;  string sessionId = 4
CLIENT_AVATAR_CLASS = "ClientAvatar"

# msgpack 空 map —— 作为最小 initDict 先试水
MSGPACK_EMPTY_MAP = b"\x80"

# 玩家实体的最小 initDict。
#
# ★ 结构必须是这样（依据 Core/PropertySync/PuppetPropertyComponent.lua:initProperty）：
#
#     local properties = dict.__Properties__      -- 没有这层包裹就直接 return
#     if properties == nil then return end
#     for name, value in pairs(properties) do
#       if type(value) == "table" then
#         local customType = value.__tp__          -- table 类型必须带 __tp__
#         assert(customType ~= nil)
#         value.__tp__ = nil
#         CustomTypeFactory.createReadonly(customType, name, value, self, nil, true)
#
#   而 customDict 的 roinit 又要求：
#     local id = value.__id__
#     value.__id__ = nil
#     assert(id ~= nil)                            -- 必须带 __id__
#
#   所以自定义字典属性的写法是 { __tp__ = "customDict", __id__ = N }。
#
# 之前直接给 {"item_bag": []}，因为没有 __Properties__ 包裹，
# initProperty 第一行就 return 了，属性一个都没设上 ——
# 于是 AvatarBagComp:refreshLocalBagData 里 pairs(self.item_bag) 拿到 nil。
#
# 后续每补一个属性就能多过一关，按客户端报错逐个加。
# item_bag 的类型是 config.lua Custom 段里的 ItemBag：
#   ItemBag = { NameSpace = "CustomTypes.Bag.ItemBag",
#               Properties = {}, __ValueType__ = "ItemInfo", __IntTypeKey__ = false }
# __tp__ 必须填**注册名**（ItemBag），不能填 PropertyCallbacks 里的类型标签（customDict）——
# 填 customDict 会报 "can not find custom class customDict"。
# ★ 完整属性表从 config.lua 的 AvatarProperties 生成（tools: work/oo_analysis/gen_initdict.py）
#
# config.lua 里 AvatarProperties.Properties 是玩家实体属性的权威声明，
# 每条形如 {propType, default, aoiscope, persist}。共 173 条：
#   45 个简单类型（int/string/double/boolean）-> 直接给默认值
#   128 个自定义类型 -> 给 { __tp__ = <类型名>, __id__ = <唯一整数> }
#
# 这份表由 game_server/avatar_init.json 提供，避免手写遗漏。
_AVATAR_INIT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "avatar_init.json")

def _load_avatar_init():
    """读取 avatar_init.json。

    ★ 必须响亮地失败：之前这个文件被写坏成 0 字节，
      静默降级成空表，导致属性全是 nil，白白排查了很久。
    """
    try:
        with open(_AVATAR_INIT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        raise RuntimeError("avatar_init.json 读取失败: %s" % e)
    props = d.get("__Properties__")
    if not isinstance(props, dict) or len(props) < 10:
        raise RuntimeError(
            "avatar_init.json 内容异常：__Properties__ 只有 %s 项（文件 %s）"
            % (len(props) if isinstance(props, dict) else props, _AVATAR_INIT_PATH))
    return d

AVATAR_INIT_DICT = _load_avatar_init()

# 旧的手写最小表，保留作为参考/兜底
_LEGACY_INIT_DICT = {
    "__Properties__": {
        "item_bag": {"__tp__": "ItemBag", "__id__": 1},
        "task_bag": {"__tp__": "TaskBag", "__id__": 2},
        "cur_activity_info": {"__tp__": "ActivityProp", "__id__": 3},
    },
}

# 每个自定义字典属性都要给 { __tp__ = <注册名>, __id__ = <唯一整数> }。
#
# 逐条补的依据都是客户端异常（Player.log）：
#   AvatarBagComp:refreshLocalBagData    -> pairs(self.item_bag)  nil
#     => item_bag, 类型 ItemBag  (__ValueType__=ItemInfo,  __IntTypeKey__=false)
#   AvatarTaskComp:getAllDoingDailyTaskItem -> self.task_bag.*  nil
#     => task_bag, 类型 TaskBag  (__ValueType__=TaskItem,  __IntTypeKey__=true)
# 实际使用的 initDict：优先用 AvatarProperties 生成的完整表
DEFAULT_INIT_DICT = AVATAR_INIT_DICT if AVATAR_INIT_DICT.get("__Properties__") else _LEGACY_INIT_DICT

def _login_tick(st):
    """登录时跑一次事件总线：

    1. 用当前等级发一条 AvatarAttr 事件
    2. 跑一次 tick() 检查链式任务

    命中的任务（**包括 hits 和 chain_hits**）都记到 _oo_pending_chain，
    等 entity_rpc 的第一条 RPC 来补完并推同步帧。

    ★ 2026-10-21 审计 P0-1：之前只记了 chain_hits，把 hits 直接丢了，
      而 apply_event 内部已经把事件标成 seen —— 结果 AvatarAttr 命中的
      6 条剧情任务永远完不成。现在 hits 一并记进去。
    """
    try:
        from ..story import events as _events
        hits = []
        lv = st.props.get("level")
        if isinstance(lv, int) and lv > 0:
            res = _events.apply_event(
                st, _events.ev_avatar_attr("level", lv, source="login"))
            hits.extend(res.hits)
            hits.extend(res.chain_hits)
        res2 = _events.tick(st)
        hits.extend(res2.chain_hits)
        # 去重保序
        hits = [int(x) for x in dict.fromkeys(hits)]
        if hits:
            st.props["_oo_pending_chain"] = hits
            _log.info("[init] 登录事件命中待完成剧情任务 %s" % hits)
    except Exception as e:
        _log.warning("[init] 登录事件 tick 失败: %s" % e)

def _live_init_dict(uid=None):
    """登录时的 initDict（雷5：整段在 per-uid 锁内执行）。

    ★ 登录初始化会写很多状态（脚手架 / 播种 / 跨天重置 / 系统解锁重算），
      同一账号重连竞态时和 RPC 并发会互相覆盖，所以入口统一加锁。
    """
    from .. import player_state as _pstate
    with _pstate.lock_for(uid):
        return _live_init_dict_impl(uid)


def _live_init_dict_impl(uid=None):
    """登录时的 initDict 用服务端可变状态（player_state）。

    这样 Set* 改过的值、以及落盘后重启的存档，都会在下次登录时生效。
    uid 不为空时把 initDict 里的 uid 覆盖成它 —— 多账号时每个账号用自己的 uid
    （现在 uid 直接复用账号名，见 game_server/server.py 的在线账号关联）。
    """
    try:
        from .. import player_state as pstate
    except Exception:
        try:
            import player_state as pstate
        except Exception:
            return DEFAULT_INIT_DICT
    try:
        # ★ 服务端状态必须用整数键（和客户端一致）。
        #   avatar_init.json 里是 "#9001" 这种写法，_conv_int_keys 之后才是 9001。
        #   以前只有发包时才转换，导致 player_state 里存的是 "#9001"，
        #   改数据、算同步的 name 时对不上。
        st = pstate.get_state(_conv_int_keys(AVATAR_INIT_DICT.get("__Properties__") or {}),
                              uid=uid)
        # ★ P0-D / 2026-10-23：每次登录重置「剧情补完」标记，
        #   让本轮登录的**首条 RPC** 重新 drain 一次 _oo_pending_chain。
        #   （旧实现不重置，靠重复跑撑正确性；现在按登录粒度精确触发。）
        pstate.reset_drain(uid)
    except Exception:
        return DEFAULT_INIT_DICT
    # 账号 uid 写进状态本身（多账号时每个账号用自己的 uid）
    if uid:
        st.props["uid"] = uid
    # ★ 脚手架覆盖层：只在「该账号首次建状态」时应用一次，默认关闭。
    #   以前是烤进 avatar_init.json 的，生成后关不掉，见 scaffold.py 头部注释。
    # 只有「新建存档」才应用脚手架（老存档里的进度不能被覆盖）
    if getattr(st, "is_new", False) and not getattr(st, "_scaffold_applied", False):
        try:
            from .. import scaffold as scaffold_mod
        except Exception:
            try:
                import scaffold as scaffold_mod
            except Exception:
                scaffold_mod = None
        if scaffold_mod is not None:
            try:
                changed = scaffold_mod.apply(st.props)
                if changed:
                    print("[init] 脚手架已应用(uid=%s): %s"
                          % (uid, "、".join(changed)), flush=True)
            except Exception as e:
                _log.warning("[init] 脚手架应用失败: %s" % e)
        st._scaffold_applied = True

    # ★ 位置持久化：把上次上报的位置写回 street_pos_info。
    #   客户端 streetsystemmgrbase.lua:561-566 读它恢复出生点。
    #   数据来源是 entity_rpc 的 _update_street_pos / _client_move
    #   存下的 _oo_street_pos。
    # ★ 位置持久化：把上次上报的位置写回 street_pos_info。
    #   street_pos_info 的类型是 DoubleDict（Dict<string,double>），
    #   客户端 streetsystemmgrbase.lua:561-566 读它恢复出生点：
    #     local posinfo = obj:getStreetPosInfo()
    #     if posinfo and posinfo.x then remotePosInfo = copy(posinfo) end
    #   :654 用 remotePosInfo.x/y/z 定位、.dir 定朝向。
    _sp = st.props.get("_oo_street_pos")
    if isinstance(_sp, dict):
        _spi = st.props.get("street_pos_info")
        if isinstance(_spi, dict):
            try:
                _spi["x"] = float(_sp.get("x") or 0)
                _spi["y"] = float(_sp.get("y") or 0)
                _spi["z"] = float(_sp.get("z") or 0)
                _spi["dir"] = float(_sp.get("dir") or 0)
                st.props["street_pos_info"] = _spi
            except Exception:
                pass

    # ★ 剧情任务播种（每次登录都跑，幂等）。
    #
    #   阶段 C 的错误（2026-10-22 实机发现）：以前这整块包在
    #   `if getattr(st, "is_new", False):` 里 —— 只有**新号**才播种。
    #   老号（比如 ootest001）永远不会补播，任务面板永远只有 1 个任务。
    #
    #   而 seed() 本身是幂等的（跳过已在 story_task_bag / done_story_tasks 的），
    #   所以每次登录都跑是正确且安全的。
    #   实机证据：服务端日志里有「[init] 章节属性补全」但没有「[story] 播种」，
    #   说明 init_new_player 根本没被调用。
    try:
        from .. import story as story_mod
    except Exception:
        try:
            import story as story_mod
        except Exception:
            story_mod = None
    if story_mod is not None:
        try:
            if not ooconf.SCAFFOLD.all_tasks_done:
                story_mod.init_new_player(st)
        except Exception as e:
            _log.warning("[init] 剧情播种失败: %s" % e)

    # ★ 修复「结算后无缝切街区」崩溃（2026-10-23 实机日志实锤）：
    #   客户端进街区 StreetSystemMgrBase.lua:111-114 getStreetBgm() 读
    #     self:getStreetInfoByID(curr_street_id)  -> street_info[street_id]
    #   而结算走 jumpToStreetStage（纯客户端，不发切街 RPC），
    #   服务端从不创建 street_info[101] -> 客户端拿 nil ->
    #   "attempt to index local streetInfo (a nil value)" FATAL -> 弹神秘力量。
    #   重登能进是因为走登录 initDict，但 initDict 的 street_info 也是空容器。
    #   所以登录时对当前 street_id 建条目，任何到街区的路径都有数据。
    try:
        from ..street.info import _street_info_of as _sio
        _sid = int(st.props.get("street_id") or 0)
        if _sid:
            _si, _entry = _sio(st, _sid)
            if _si is not None:
                print("[init] 确保 street_info[%s] 条目存在%s"
                      % (_sid, "（新建）" if _entry else ""), flush=True)
    except Exception as e:
        _log.warning("[init] street_info 条目补齐失败: %s" % e)

    # ★ 2026-10-23 实机反馈：街区「队伍切换」头像显示男主。
    #   客户端 StreetV2Panel:initFormation 用 character_list[choosen_index].value[i]
    #   （charId）当队员头像（CharData[charId].DefaultSkin -> CharAppearanceData.
    #   FacialDrawing）。老号（取名早于编队修复）的编队仍可能是 [9001,0,0]，
    #   即使 gender=2 / character_choosen_char=9002。
    #   登录时统一按 gender 修正：主角 + 编队 + characterBag（幂等，无帧，快照自带）。
    if int(st.props.get("gender") or 0) in (1, 2):
        try:
            from ..avatar import _ensure_hero_by_gender as _eh, _set_formation_hero as _sfh
            _eh(st, st.props.get("gender"), None)
            _want = 9002 if int(st.props.get("gender")) == 2 else 9001
            _sfh(st, _want, None)
            print("[init] 按 gender=%s 校正主角/编队（want=%s）"
                  % (st.props.get("gender"), _want), flush=True)
        except Exception as e:
            _log.warning("[init] 主角/编队校正失败: %s" % e)


    # ★ P1 阶段 N-d：跨天重置（每日任务 / 头衔日常）。
    #   只在「天序号变了」时才动，同一天多次登录幂等。
    #   必须在通用任务播种**之前**跑，否则重置会把刚播的日常任务又清掉。
    try:
        from .. import tasks as _tasks_d
        _rt, _rch = _tasks_d.daily_reset(st)
        if _rt:
            print("[init] 跨天重置 %s（%d 条同步描述，已进初始快照）"
                  % (list(_rt), len(_rch)), flush=True)
    except Exception as e:
        _log.warning("[init] 跨天重置失败: %s" % e)


    # ★ P1 阶段 N-a2：通用任务播种（TaskData 那套 -> task_bag）。
    #
    #   实机取证（2026-10-22）：task_bag 之前实测 0 槽位、0 任务，
    #   于是 getSystemTaskState(任意) 恒 -1，
    #   而 AllSystemCatalogueData 里 40 条 unlockType=1 的系统入口
    #   判的就是 getSystemTaskState(1) == Finished -> 数据条件永假。
    #
    #   ★ 这里在「快照返回之前」跑（_public 在本函数末尾才构建），
    #     所以播进去的任务直接进初始快照，不需要额外推帧。
    #   seed_tasks 是幂等的（跳过已在 task_bag / done_tasks 的）。
    try:
        from .. import tasks as _tasks_mod
        _gt = _tasks_mod.seed_tasks(st)
        if _gt:
            print("[init] 通用任务播种：%d 条同步描述（已进初始快照）" % len(_gt),
                  flush=True)
    except Exception as e:
        _log.warning("[init] 通用任务播种失败: %s" % e)

    # ★ P1 阶段 G：章节收集数 / 奖励列表补全（登录时推一次）。
    #   客户端 avatarstorylinecomp.lua:97-107 读这三个属性，
    #   以前服务端从来不写 -> 章节 UI 的收集进度/奖励状态恒为空。
    try:
        from .. import story as _story_mod
        try:
            _sl = _story_mod.sync_story_max_collect_num(st)
            _sl += _story_mod.sync_story_reward_list(st)
            if _sl:
                _log.info("[init] 章节属性补全 %d 项" % len(_sl))
        except Exception as e:
            _log.warning("[init] 章节属性补全失败: %s" % e)
    except Exception:
        pass

    # ★ 体力：新号拉满；每次登录按离线时间补。
    #   以前 tower_reward_keys 默认 0、两个时间戳也 0，
    #   导致「开局不满、也不随时间恢复」。见 game_server/stamina.py。
    try:
        from .. import stamina as stamina_mod
    except Exception:
        try:
            import stamina as stamina_mod
        except Exception:
            stamina_mod = None
    if stamina_mod is not None:
        try:
            _lv = int(st.props.get("level") or 1)
            if getattr(st, "is_new", False) and not getattr(st, "_stamina_inited", False):
                stamina_mod.init_new(st.props, _lv)
                st._stamina_inited = True
                print("[init] 体力初始化(uid=%s): %s/%s"
                      % (uid, st.props.get("tower_reward_keys"),
                         stamina_mod.max_stamina(_lv)), flush=True)
            else:
                _gained, _ = stamina_mod.apply_recovery(st.props, _lv)
                if _gained:
                    print("[init] 离线恢复体力(uid=%s): +%s -> %s"
                          % (uid, _gained, st.props.get("tower_reward_keys")), flush=True)
        except Exception as e:
            _log.warning("[init] 体力处理失败: %s" % e)
    # ★ 服务端私有字段（以 _oo_ 开头，比如 _oo_street_pos / _oo_micro_state）
    #   **绝不能进 initDict** —— 客户端 initProperty 对每个表值都要求带 __tp__
    #   （core/propertysync/puppetpropertycomponent.lua:51-54 的 assert），
    #   普通 dict 会让 ClientAvatar:init 抛错、返回非布尔，
    #   表现为「自动进入游戏 -> 被踢回开始页 -> 再自动进 -> 再被踢」的死循环。
    #   实测踩过（2026-10-20 阶段 2 第 2 轮）。
    # ★ 2026-10-21 审计 P0-1：**嵌套的 __tp__ 也必须剥掉**。
    #   之前只过滤顶层 _oo_，嵌套的 __tp__ 会原样进 initDict：
    #     task_bag[1] = {"__tp__": "TaskBag", "__id__": ..., 1: {...}}
    #   客户端 initProperty -> TaskBag:roinit 遍历到 __tp__ 这个键 ->
    #     declare = __ValueTypeDeclare__（TaskItem，自定义类型）->
    #     createCustomObjRO("__tp__", "TaskBag") -> 对字符串执行
    #     value.__id__ = nil（customdict.lua:449）-> 崩
    #   实测：接一个通用任务后重新登录，客户端必崩。
    #
    #   _strip_private(is_root=True) 保留根属性值的 __tp__（客户端靠它认类型），
    #   递归进去的值一律剥掉。
    # ★ P0-D：登录时跑一次事件总线（服务端主动推进，不再只在副本结算时求值）
    _login_tick(st)

    # ★ roadmap 队列第 2 项：登录时**全量重算系统解锁**。
    #   为什么要在登录做：
    #     - 玩家离线期间可能完成任务 / 升级，内存里的 unlocked_system 会落后；
    #     - `_recalc_system_unlocks` 是唯一写入口，登录补一次能自愈任何历史遗漏；
    #     - 这里在快照构建之前，所以重算结果直接进 initDict，不需要推帧。
    try:
        from ..story.rpc import _recalc_system_unlocks as _recalc_u
        _uf = _recalc_u(st)
        if _uf:
            print("[init] 系统解锁重算：%d 条（已进初始快照）" % len(_uf),
                  flush=True)
    except Exception as e:
        _log.warning("[init] 系统解锁重算失败: %s" % e)

    from ..property import sync as _syncmod
    _public = {}
    for k, v in st.props.items():
        if isinstance(k, str) and k.startswith("_oo_"):
            continue
        _public[k] = _syncmod._strip_private(v, is_root=True)
    return {"__Properties__": _public}

def build_channel_entity(entity_type=CLIENT_AVATAR_CLASS,
                         init_dict=None,
                         entity_id=_DEF_AVATAR_EID,
                         session_id=_DEF_UID,
                         uid=None):
    """构造 createChannelEntity（IGateClientService 第 4 个方法）。

    字段 1 按 **string** 发类名（如 "ClientAvatar"）；
    字段 2 是 msgpack 编码的 initDict。
    """
    if not isinstance(entity_type, bytes):
        entity_type = str(entity_type).encode("utf-8")
    if init_dict is None:
        init_dict = _live_init_dict(uid)
    info = mp_map(_conv_int_keys(init_dict)) if init_dict else MSGPACK_EMPTY_MAP
    body = pb_bytes_field(1, entity_type)
    body += pb_bytes_field(2, info)
    body += pb_bytes_field(3, entity_id.encode("utf-8"))
    body += pb_bytes_field(4, session_id.encode("utf-8"))
    return frame(4, body)
