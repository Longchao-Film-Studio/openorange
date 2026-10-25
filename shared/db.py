#!/usr/bin/env python3
"""shared.db —— OpenOrange 共享数据层(库, 非服务)

web / game_server 都 import 本模块, 直接读写同一个 SQLite 文件。
使用 WAL 模式以支持多进程并发。

表结构:
  accounts  玩家账号(**必须在网页注册**)
  tickets   登录票据(login 签发 -> 供游戏服校验)
  sessions  网页登录会话

账号策略:
  - 账号名 = 登录标识(UID): 4~20 位字母 / 数字 / 下划线, 不验证真实性
  - 密码用 PBKDF2-SHA256 加盐哈希存储
  - 一个账号同时用于网页登录与启动器登录
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

from shared.log_tee import get_logger

_log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认用 shared/data.db；设 OO_DB_PATH 可换库（测试、多开都用得上）。
DB_PATH = os.environ.get("OO_DB_PATH") or os.path.join(BASE_DIR, "data.db")

PBKDF2_ROUNDS = 120000

# ★ 雷6（2026-10-23）：票据 TTL。
#   票据同时用于游戏登录和游戏内指令通道（客户端把 pc.ticket 一直复用，
#   见 launcher/lua/oo/03_command.lua:114/82）。所以**不能**做一次性消费，
#   否则第一条命令之后全部 401。
#   这里把有效期从 24h 缩到 8h（覆盖一次长时间游玩），把泄露窗口缩小 3 倍；
#   另外 /api/chat/command 加了按账号限流（见 web/ratelimit.py）。
TICKET_TTL = 8 * 3600

_local = threading.local()


def _conn():
    """每线程一个连接(SQLite 连接不可跨线程共享)。"""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=15000")
        _local.conn = c
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT    NOT NULL UNIQUE,   -- 账号名(登录标识/UID)
    pass_salt         TEXT,                      -- 密码盐(旧库可能为空)
    pass_hash         TEXT,                      -- PBKDF2 哈希
    account_id        TEXT    NOT NULL UNIQUE,   -- fp-passport account_id
    fp_uid            TEXT    NOT NULL,          -- FP 玩家 UID
    session_key       TEXT    NOT NULL,
    channel_uid       TEXT,
    is_formal         INTEGER NOT NULL DEFAULT 1,
    is_new            INTEGER NOT NULL DEFAULT 1,
    perm_level        INTEGER NOT NULL DEFAULT 0,  -- 0=玩家 1=GM 2=管理员
    created_ts        INTEGER NOT NULL,
    last_login_ts     INTEGER,
    login_count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket      TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    issued_ts   INTEGER NOT NULL,
    expire_ts   INTEGER NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    issued_ts   INTEGER NOT NULL,
    expire_ts   INTEGER NOT NULL
);

-- 游戏内在线表：由 game_server 在收到 createChannelEntity / 心跳 / 断连时维护。
-- 以前 /who 读的是 sessions（网页会话），启动器每登一次留一条，越积越多。
CREATE TABLE IF NOT EXISTS game_online (
    session_id   TEXT PRIMARY KEY,   -- 游戏服连接标识（服务端自增连接号）
    username     TEXT,               -- 账号名（来自 ms GameLoadService.query）
    uid          TEXT,               -- 游戏内 uid，如 OO00000001
    entity_id    TEXT,               -- 实体 id，如 avatar_2000
    addr         TEXT,               -- 客户端地址
    since_ts     INTEGER NOT NULL,   -- 本次上线时间
    last_seen_ts INTEGER NOT NULL    -- 最后一次心跳/活动时间
);
"""


def init():
    """建表(幂等) + 旧库补列。"""
    c = _conn()
    c.executescript(SCHEMA)
    # 旧库(没有 pass_salt/pass_hash 列)平滑升级
    cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)").fetchall()}
    if "pass_salt" not in cols:
        c.execute("ALTER TABLE accounts ADD COLUMN pass_salt TEXT")
    if "pass_hash" not in cols:
        c.execute("ALTER TABLE accounts ADD COLUMN pass_hash TEXT")
    if "perm_level" not in cols:
        c.execute("ALTER TABLE accounts ADD COLUMN perm_level INTEGER NOT NULL DEFAULT 0")
    # 旧库列改名：账号名不再限定手机号，mobile -> username。
    for table in ("accounts", "sessions", "tickets"):
        tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        if "mobile" in tcols and "username" not in tcols:
            c.execute(f"ALTER TABLE {table} RENAME COLUMN mobile TO username")
    # 早期版本的「游戏内取验证码」流程已废弃，旧库里的 codes 表一并清掉。
    c.execute("DROP TABLE IF EXISTS codes")
    c.commit()
    return DB_PATH


# ---------------- 密码 ----------------

def hash_password(password, salt=None):
    """返回 (salt_hex, hash_hex)。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password, salt_hex, hash_hex):
    """常量时间比对。"""
    if not salt_hex or not hash_hex:
        return False
    try:
        _, calc = hash_password(password, salt_hex)
    except Exception:
        return False
    return hmac.compare_digest(calc, hash_hex)


# ---------------- accounts ----------------

def get_account_by_account_id(account_id):
    """按 account_id（oo_+sha256）查账号。

    游戏服的 ms GameLoadService.query 里带的是 account_id 而不是登录名，
    要还原成登录名就得靠这个查询。
    """
    if not account_id:
        return None
    r = _conn().execute("SELECT * FROM accounts WHERE account_id=?",
                        (str(account_id),)).fetchone()
    return dict(r) if r else None


def get_account_by_username(username):
    r = _conn().execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def register_account(username, password, account_id, fp_uid, session_key, channel_uid=None, is_formal=1):
    """网页注册。已存在则返回 None(由调用方判断)。"""
    if get_account_by_username(username):
        return None
    salt, phash = hash_password(password)
    now = int(time.time())
    c = _conn()
    try:
        c.execute(
            "INSERT INTO accounts(username,pass_salt,pass_hash,account_id,fp_uid,session_key,"
            "channel_uid,is_formal,is_new,created_ts,last_login_ts,login_count) "
            "VALUES(?,?,?,?,?,?,?,?,1,?,?,0)",
            (username, salt, phash, account_id, fp_uid, session_key,
             channel_uid or username, is_formal, now, now),
        )
        c.commit()
    except sqlite3.IntegrityError:
        return None
    return get_account_by_username(username)


def set_password(username, password):
    salt, phash = hash_password(password)
    c = _conn()
    c.execute("UPDATE accounts SET pass_salt=?, pass_hash=? WHERE username=?", (salt, phash, username))
    c.commit()


def touch_login(username, session_key=None):
    """更新最后登录时间/次数(可选刷新 session_key)。"""
    now = int(time.time())
    c = _conn()
    if session_key:
        c.execute("UPDATE accounts SET last_login_ts=?, login_count=login_count+1, session_key=? WHERE username=?",
                  (now, session_key, username))
    else:
        c.execute("UPDATE accounts SET last_login_ts=?, login_count=login_count+1 WHERE username=?",
                  (now, username))
    c.commit()


def mark_not_new(username):
    _conn().execute("UPDATE accounts SET is_new=0 WHERE username=?", (username,))
    _conn().commit()


def delete_account(username):
    c = _conn()
    c.execute("DELETE FROM sessions WHERE username=?", (username,))
    c.execute("DELETE FROM tickets WHERE username=?", (username,))
    c.execute("DELETE FROM accounts WHERE username=?", (username,))
    c.commit()


# ---------------- 网页会话 ----------------

def create_session(username, ttl=86400 * 7):
    """网页登录成功 -> 生成会话 token（允许多端同时在线）。"""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    c = _conn()
    c.execute("INSERT INTO sessions(token,username,issued_ts,expire_ts) VALUES(?,?,?,?)",
              (token, username, now, now + ttl))
    c.commit()
    return token


def drop_other_sessions(username, keep_token):
    """改密后踢掉其它端，只保留当前会话。"""
    c = _conn()
    c.execute("DELETE FROM sessions WHERE username=? AND token<>?", (username, keep_token or ""))
    c.commit()


def get_session(token):
    r = _conn().execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not r:
        return None
    if int(time.time()) > r["expire_ts"]:
        return None
    return dict(r)


def drop_session(token):
    c = _conn()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()


def list_sessions(limit=50):
    return [dict(x) for x in _conn().execute(
        "SELECT * FROM sessions ORDER BY issued_ts DESC LIMIT ?", (limit,)).fetchall()]

# ---------------- tickets ----------------

def issue_ticket(ticket, username, account_id, ttl=None):
    ttl = TICKET_TTL if ttl is None else int(ttl)
    now = int(time.time())
    c = _conn()
    c.execute("INSERT OR REPLACE INTO tickets(ticket,username,account_id,issued_ts,expire_ts,consumed) "
              "VALUES(?,?,?,?,?,0)", (ticket, username, account_id, now, now + ttl))
    c.commit()


def get_ticket(ticket):
    r = _conn().execute("SELECT * FROM tickets WHERE ticket=?", (ticket,)).fetchone()
    return dict(r) if r else None


# ---------------- 权限 / 在线 ----------------

def get_perm_level(username):
    """取账号权限等级：0=玩家 1=GM 2=管理员。账号不存在返回 -1。"""
    r = _conn().execute("SELECT perm_level FROM accounts WHERE username=?", (username,)).fetchone()
    if not r:
        return -1
    return int(r["perm_level"] or 0)


def set_perm_level(username, level):
    c = _conn()
    cur = c.execute("UPDATE accounts SET perm_level=? WHERE username=?", (int(level), username))
    c.commit()
    return cur.rowcount > 0


# 心跳间隔约 1 秒，超过这个秒数没动静就认为掉线了。
ONLINE_STALE_SEC = 120


def set_game_online(session_id, username=None, uid=None, entity_id=None, addr=None):
    """标记一个游戏连接上线（由 game_server 调用）。

    同一个 session_id 重复调用会刷新 last_seen_ts，不会重复插入。
    """
    now = int(time.time())
    c = _conn()
    c.execute(
        "INSERT INTO game_online (session_id, username, uid, entity_id, addr, since_ts, last_seen_ts) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  username=COALESCE(excluded.username, game_online.username), "
        "  uid=COALESCE(excluded.uid, game_online.uid), "
        "  entity_id=COALESCE(excluded.entity_id, game_online.entity_id), "
        "  addr=COALESCE(excluded.addr, game_online.addr), "
        "  last_seen_ts=excluded.last_seen_ts",
        (str(session_id), username, uid, entity_id, addr, now, now))
    c.commit()
    return True


def touch_game_online(session_id):
    """刷新 last_seen_ts（心跳时调用）。连接已不存在则忽略。"""
    c = _conn()
    c.execute("UPDATE game_online SET last_seen_ts=? WHERE session_id=?",
              (int(time.time()), str(session_id)))
    c.commit()


def clear_game_online(session_id):
    """标记下线（连接关闭时调用）。"""
    c = _conn()
    c.execute("DELETE FROM game_online WHERE session_id=?", (str(session_id),))
    c.commit()


def list_game_online(stale_sec=ONLINE_STALE_SEC, limit=200):
    """真正的游戏内在线玩家：game_online 里最近有动静的。"""
    now = int(time.time())
    rows = _conn().execute(
        "SELECT g.*, a.perm_level, a.last_login_ts "
        "FROM game_online g LEFT JOIN accounts a ON a.username = g.username "
        "WHERE g.last_seen_ts > ? ORDER BY g.since_ts DESC LIMIT ?",
        (now - int(stale_sec), limit)).fetchall()
    return [dict(r) for r in rows]


def online_accounts(limit=200):
    """在线玩家（游戏内）。

    历史：这里以前读 sessions（网页会话），而启动器每登录一次就留一条、
    过期时间很长，导致 /who 把历史登录全列出来（同一个账号 8 条）。
    现在改读 game_online —— 由 game_server 在 createChannelEntity / 心跳 /
    断连时维护，只有真正进了游戏的连接才会出现。
    """
    return list_game_online(limit=limit)


def list_accounts(limit=100):
    return [dict(x) for x in _conn().execute(
        "SELECT * FROM accounts ORDER BY last_login_ts DESC LIMIT ?", (limit,)).fetchall()]


def stats():
    c = _conn()
    now = int(time.time())
    return {
        "accounts": c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"],
        "tickets": c.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"],
        "sessions": c.execute("SELECT COUNT(*) n FROM sessions WHERE expire_ts>?", (now,)).fetchone()["n"],
        "db_path": DB_PATH,
    }


if __name__ == "__main__":
    _log.info("DB:", init())
    _log.info("stats:", stats())