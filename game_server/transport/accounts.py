"""多账号：gate 连接 <-> 账号 的关联（P0 遗留清理：从 server.py 拆出）。

客户端登录顺序是固定的：ms 上先发 GameLoadService.query（带 Username），
然后 gate 建新连接（reqType=0）。所以用「待关联队列 + 时间窗」把它们对上。

★ 局限（遗留第 4 条的根源）：两个客户端**同时**启动时可能串号（先后启动没问题）。
  要彻底解决得让客户端把账号带进 connectServer 的 auth 字段 —— 见
  transport/gate.py 里 ownerid 的用法。
"""
import contextlib
import threading
import time

from shared.log_tee import get_logger

from .. import ms as msmod
from ..config import FALLBACK_UID as DEFAULT_UID

_log = get_logger(__name__)

try:
    from shared import db as shared_db
except Exception:
    shared_db = None

_LOCK = threading.Lock()
_UID_LOCK = threading.Lock()   # _gate_uid 自己的锁（读改写要和 _pending 分开）
_pending = []          # [(ts, username)]
_gate_uid = {}         # cid -> username（= 游戏内 uid）
USERNAME_MATCH_WINDOW = 30       # 秒


def resolve_display_uid(raw):
    """把 ms query 里的 Username 还原成玩家看得懂的 uid。

    ms GameLoadService.query 里的 Username 其实是 **account_id**
    （oo_ + sha256("acct:"+登录名) 前 16 位），例如
    oo_08bed76013b06783 对应登录名 15900159000。
    所以查库换回真正的登录名；查不到才退回原始串。
    """
    if not raw:
        return None
    if shared_db is not None:
        try:
            acc = shared_db.get_account_by_account_id(raw)
            if acc:
                name = (acc.get("username") or "").strip()
                if name:
                    return name
                print(f"[GATE] 警告：account_id={raw} 的账号没有登录名，只能退回原始串", flush=True)
            else:
                print(f"[GATE] 警告：account_id={raw} 在库里查不到，只能退回原始串",
                      flush=True)
        except Exception as _e:
            _log.warning(f"[GATE] 警告：还原登录名失败 {_e}")
    # ★ 不用数字兜底：uid 必须是登录名。查不到才退回原始串（正常不会走到）。
    return raw


def remember_username(username):
    """ms query 里看到的账号，还原成 uid 后放进待关联队列。"""
    if not username:
        return
    username = resolve_display_uid(username)
    if not username:
        return
    now = time.time()
    with _LOCK:
        _pending.append((now, username))
        # 只留窗口内的，防止无限增长
        while _pending and now - _pending[0][0] > USERNAME_MATCH_WINDOW * 4:
            _pending.pop(0)


def claim_username():
    """给一条新 gate 连接领一个账号名（最早的、窗口内的）。领不到返回 None。"""
    now = time.time()
    with _LOCK:
        while _pending:
            ts, name = _pending.pop(0)
            if now - ts <= USERNAME_MATCH_WINDOW:
                return name
        return None


def claim_and_bind(cid):
    """reqType=0 的连接：领一个账号并绑定，**在同一把锁里完成**。

    ★ 审计指出：以前 gate 里是 is_bound(cid) + claim_username() + bind(cid, ...)
      三次独立调用，check-then-act 不是原子的；文档却写「在同一把锁里」。
      现在合成一个函数，锁内完成「已绑就不动 / 否则领一个（可能 None）并落键」。
    """
    with _UID_LOCK:
        if cid in _gate_uid:
            return _gate_uid[cid]
        now = time.time()
        claimed = None
        with _LOCK:
            while _pending:
                ts, name = _pending.pop(0)
                if now - ts <= USERNAME_MATCH_WINDOW:
                    claimed = name
                    break
        _gate_uid[cid] = claimed
        return claimed


def bind(cid, uid):
    """把 cid 和 uid 绑起来（新连接领号 / 从 auth 还原 都走这里）。

    ★ 即使 uid 是 None 也要落键。审计实测：父提交里是
      _gate_uid[cid] = _claimed（无条件写），如果这里「uid 为空就不写」，
      同一个 cid 再收到一次 reqType=0 时 is_bound() 仍是 False，
      会把**另一个账号**再领一遍并覆盖，造成串号。
    """
    with _UID_LOCK:
        _gate_uid[cid] = uid
    return _gate_uid.get(cid)


def uid_of(cid):
    """这条 gate 连接对应的 uid（= 账号名）。没有就回退默认值。"""
    with _UID_LOCK:
        return _gate_uid.get(cid) or DEFAULT_UID


def is_bound(cid):
    with _UID_LOCK:
        return cid in _gate_uid


def release(cid):
    """连接断开时释放关联。"""
    with _UID_LOCK:
        _gate_uid.pop(cid, None)


def uid_from_auth(fields):
    """从 connectServer 的 auth 字段里还原账号 uid。

    绑定链的每一步都是**新连接**（新 cid），所以账号不能只靠 cid 记住。
    好在 bindAccount/bindAvatar 下发的 mailbox 名字客户端会原样回传在 auth 里：
      reqType=4 -> auth = Mailbox{1:"account_<uid>", 2:ip, 3:port, 4:...}
      reqType=3 -> auth = Mailbox{1:"avatar_<uid>",  ...}
    所以剥掉前缀就能把账号带过整条链路。

    ★ P0-账号绑定（2026-10-23）：reqType=0（NEW_CONNECTION）也被注入 Lua 补了
      Mailbox{1:"account_<account_id>"}，所以这里同时是 reqType=0 的认号入口。
    """
    raw = fields.get(5)
    if not raw:
        return None
    with contextlib.suppress(Exception):
        mb = msmod.pb_parse(raw[0])
        name = mb.get(1, [b""])[0]
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        for p in ("account_", "avatar_"):
            if name.startswith(p):
                return name[len(p):]
    return None


def uid_from_auth_display(fields):
    """从 auth 还原出**登录名**（游戏内 uid）。

    auth 里可能是 account_id（oo_xxx，注入 Lua 填的）也可能是登录名
    （旧链路 bindAccount 回传的），统一再过一遍 resolve_display_uid：
      真实路径 = uid_from_auth(fields) -> resolve_display_uid(...)
    这样无论客户端塞哪种标识，下游 _state(uid) / 实体 id 都落在登录名上。
    """
    uid = uid_from_auth(fields)
    if not uid:
        return None
    return resolve_display_uid(uid) or uid

