#!/usr/bin/env python3
"""跨业务域共用的 RPC 基础设施（P0-E2）。

拆 entity_rpc.py 时发现，有四个东西几乎每个业务域都要用：

    state(uid)          服务端可变玩家状态（懒加载）
    sync_frame(...)     统一属性同步帧构造器
    throttled_save(...) 节流落盘
    decode_params(...)  msgpack 参数解码

如果把它们留在 entity_rpc.py，拆出去的模块就得反过来 import 装配层，
立刻变成循环依赖。所以单独抽一层，依赖方向固定为：

    业务域模块  ->  rpc_common  ->  ms / property / player_state
    装配层(dispatch) -> 各业务域模块

"""
import time

from . import ms as msmod
from .property import sync as _sync

# 事件链递归深度上限（防御外部数据里的环）
EVENT_MAX_DEPTH = 8

# 位置上报很频繁（每次移动都发），不能每次都写盘。
# 同一个 uid 每 5 秒最多落盘一次；正常退出/切场景时 force 写。
_LAST_SAVE = {}
_SAVE_INTERVAL = 5.0


def decode_params(params):
    """把 msgpack 参数解成 Python 值，解不出来就返回 None。"""
    if not params:
        return None
    try:
        val, _ = msmod.mp_decode(params)
        return val
    except Exception:
        return None


def state(uid=None):
    """服务端可变玩家状态（按 uid 分开，懒加载，首次用 initDict 初始化）。"""
    from . import player_state as pstate
    # 和 ms._live_init_dict 保持一致：状态里一律用整数键
    props = msmod._conv_int_keys(msmod.AVATAR_INIT_DICT.get("__Properties__") or {})
    return pstate.get_state(props, uid=uid or pstate.DEFAULT_UID)


# 属性同步帧里的 ownerid 语义见 property/sync.py 的 SYNC_SELF 注释。
# 这里 re-export，业务模块从 rpc_common 或 property.sync 取都可以。
from shared.log_tee import get_logger

from .property.sync import SYNC_SELF  # noqa: F401  (re-export)

_log = get_logger(__name__)


def sync_frame(ownerid, property_id, op_type, name, value, session_id=""):
    """统一的属性同步帧构造器 —— 所有产生同步帧的地方都应该用它。

    ownerid: ""（SYNC_SELF）表示「就是收帧的这个玩家自己」；非空表示别人的 uid。

    ★ 为什么要有这层（2026-10-21 审计 P0-1/P0-2/P0-3）：
      原先 entity_rpc.py 里有 30 处直接调 msmod.build_property_sync，
      它们绕过了 sync 层的剥离和校验，于是：
        - 写嵌套容器时把 __tp__ 发出去 -> 客户端 rochange/roinit 崩
        - name 是 int -> ms.pb_bytes_field 里 len(int) 抛 TypeError
        - 非法帧照发，没人拦

    这层做三件事：
      1. 剥私有键（_oo_* 永远剥；__tp__ 只在 propertyId=0 的根属性值里保留）
      2. name 统一转字符串
      3. 不做校验（校验在 sync.validate，测试里跑 strict 模式）

    参数顺序和 msmod.build_property_sync 完全一致，方便整段替换。
    """
    if ownerid is None:
        ownerid = SYNC_SELF
    elif not isinstance(ownerid, str):
        # ★ 不要静默 str()：传 int 会变成 "123"，客户端 getRealOwner("123")
        #   找不到就把整帧丢掉 —— 比直接报错更难排查。所以这里显式拒绝。
        raise TypeError(f"ownerid 必须是 str（空串 = 收帧者自己），收到 {ownerid!r}")
    if op_type in (msmod.OP_DEL, msmod.OP_REMOVE):
        val = None
    else:
        # 只有根属性值（propertyId == 0）保留 __tp__
        val = _sync._strip_private(value, is_root=(int(property_id) == 0))
    if not isinstance(name, str):
        name = str(name)
    return msmod.build_property_sync(ownerid, property_id, op_type, name,
                                     val, session_id)


def respond_after_sync(rid, syncs=None, ret=None, resp=None, tail=None):
    """构造「先属性同步、后应答」的帧列表 —— 唯一入口（R1/R3）。

    ★ 不变量（不许违反）：
      客户端按【到达顺序】同步处理帧。core/client/gateclientrpchandler.lua:
      entityMessageResponse 一收到应答帧就 SafeCallback(callback, ...) 执行回调。
      因此「应答回调里需要读到的新属性值」的同步帧，必须排在该应答帧【之前】。

    反例（真实事故，2026-10-23「铁窗泪」）：
      RequestEnterStreetSurface_CS 旧写法 return [应答] + syncs，
      客户端在应答回调里读 street_info.surface_enter_time 做昼夜一致性判定
      （streetsystemmgrbase.lua:tryChangeWorld），读到旧值 0 ->
      error("服务器和客户端数据不同步") -> return -> switchWorld 不执行 ->
      昼夜不切、对话队列中断、任务停摆、剧情重播。

    用法：
        syncs = _set_xxx(st, ...)          # 先改状态并收集同步帧
        return respond_after_sync(rid, syncs)

    resp：应答的 payload（build_entity_response 的第 3 参），透传给客户端
      （目前进副本 / 伊甸聊天历史用到：应答里带 spaceInfo / ChatInfo）。
      目前只有副本结算的 AvatarSpace_SpaceFinish_SC 用到它：
      客户端日志实测顺序是 sendFinishInfo -> sendFinishInfoCallback(应答)
      -> AvatarSettlementComp OnReceiveEndMsg(推送)，即先 ACK 后推送。
      所以这条推送不能塞进 syncs（那样会跑到应答前面），只能走 tail。

    不要写成 frames = [build_entity_response(...)]; frames.append(sync) ——
    那样同步帧就落在应答之后了。
    """
    from .rpc_index import ACK_OK as _ACK_OK
    if ret is None:
        ret = dict(_ACK_OK)
    frames = list(syncs) if syncs else []
    frames.append(msmod.build_entity_response(rid, ret=ret, resp=resp))
    if tail:
        frames.extend(tail)
    return frames

def throttled_save(st, uid, force=False):
    """节流落盘：同一账号 5 秒内最多写一次。"""
    key = uid or ""
    now = time.time()
    if not force and (now - _LAST_SAVE.get(key, 0)) < _SAVE_INTERVAL:
        return
    _LAST_SAVE[key] = now
    try:
        st.save()
    except Exception as e:
        _log.warning(f"[save] 落盘失败 {key}: {e}")
