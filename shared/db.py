#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared.db —— OpenOrange 共享数据层(库, 非服务)

web / game_server 都 import 本模块, 直接读写同一个 SQLite 文件。
使用 WAL 模式以支持多进程并发。

表结构:
  accounts  玩家账号(**必须在网页注册**)
  tickets   登录票据(login 签发 -> 供游戏服校验)
  sessions  网页登录会话

账号策略:
  - 手机号 = 账号名(UID), 不验证真实性, 只校验格式
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认用 shared/data.db；设 OO_DB_PATH 可换库（测试、多开都用得上）。
DB_PATH = os.environ.get("OO_DB_PATH") or os.path.join(BASE_DIR, "data.db")

PBKDF2_ROUNDS = 120000

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
    mobile            TEXT    NOT NULL UNIQUE,   -- 手机号(登录标识/UID)
    pass_salt         TEXT,                      -- 密码盐(旧库可能为空)
    pass_hash         TEXT,                      -- PBKDF2 哈希
    account_id        TEXT    NOT NULL UNIQUE,   -- fp-passport account_id
    fp_uid            TEXT    NOT NULL,          -- FP 玩家 UID
    session_key       TEXT    NOT NULL,
    channel_uid       TEXT,
    is_formal         INTEGER NOT NULL DEFAULT 1,
    is_new            INTEGER NOT NULL DEFAULT 1,
    created_ts        INTEGER NOT NULL,
    last_login_ts     INTEGER,
    login_count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket      TEXT PRIMARY KEY,
    mobile      TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    issued_ts   INTEGER NOT NULL,
    expire_ts   INTEGER NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    mobile      TEXT NOT NULL,
    issued_ts   INTEGER NOT NULL,
    expire_ts   INTEGER NOT NULL
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

def get_account_by_mobile(mobile):
    r = _conn().execute("SELECT * FROM accounts WHERE mobile=?", (mobile,)).fetchone()
    return dict(r) if r else None


def register_account(mobile, password, account_id, fp_uid, session_key, channel_uid=None, is_formal=1):
    """网页注册。已存在则返回 None(由调用方判断)。"""
    if get_account_by_mobile(mobile):
        return None
    salt, phash = hash_password(password)
    now = int(time.time())
    c = _conn()
    try:
        c.execute(
            "INSERT INTO accounts(mobile,pass_salt,pass_hash,account_id,fp_uid,session_key,"
            "channel_uid,is_formal,is_new,created_ts,last_login_ts,login_count) "
            "VALUES(?,?,?,?,?,?,?,?,1,?,?,0)",
            (mobile, salt, phash, account_id, fp_uid, session_key,
             channel_uid or mobile, is_formal, now, now),
        )
        c.commit()
    except sqlite3.IntegrityError:
        return None
    return get_account_by_mobile(mobile)


def set_password(mobile, password):
    salt, phash = hash_password(password)
    c = _conn()
    c.execute("UPDATE accounts SET pass_salt=?, pass_hash=? WHERE mobile=?", (salt, phash, mobile))
    c.commit()


def touch_login(mobile, session_key=None):
    """更新最后登录时间/次数(可选刷新 session_key)。"""
    now = int(time.time())
    c = _conn()
    if session_key:
        c.execute("UPDATE accounts SET last_login_ts=?, login_count=login_count+1, session_key=? WHERE mobile=?",
                  (now, session_key, mobile))
    else:
        c.execute("UPDATE accounts SET last_login_ts=?, login_count=login_count+1 WHERE mobile=?",
                  (now, mobile))
    c.commit()


def mark_not_new(mobile):
    _conn().execute("UPDATE accounts SET is_new=0 WHERE mobile=?", (mobile,))
    _conn().commit()


def delete_account(mobile):
    c = _conn()
    c.execute("DELETE FROM sessions WHERE mobile=?", (mobile,))
    c.execute("DELETE FROM tickets WHERE mobile=?", (mobile,))
    c.execute("DELETE FROM accounts WHERE mobile=?", (mobile,))
    c.commit()


# ---------------- 网页会话 ----------------

def create_session(mobile, ttl=86400 * 7):
    """网页登录成功 -> 生成会话 token（允许多端同时在线）。"""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    c = _conn()
    c.execute("INSERT INTO sessions(token,mobile,issued_ts,expire_ts) VALUES(?,?,?,?)",
              (token, mobile, now, now + ttl))
    c.commit()
    return token


def drop_other_sessions(mobile, keep_token):
    """改密后踢掉其它端，只保留当前会话。"""
    c = _conn()
    c.execute("DELETE FROM sessions WHERE mobile=? AND token<>?", (mobile, keep_token or ""))
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

def issue_ticket(ticket, mobile, account_id, ttl=86400):
    now = int(time.time())
    c = _conn()
    c.execute("INSERT OR REPLACE INTO tickets(ticket,mobile,account_id,issued_ts,expire_ts,consumed) "
              "VALUES(?,?,?,?,?,0)", (ticket, mobile, account_id, now, now + ttl))
    c.commit()


def get_ticket(ticket):
    r = _conn().execute("SELECT * FROM tickets WHERE ticket=?", (ticket,)).fetchone()
    return dict(r) if r else None


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
    print("DB:", init())
    print("stats:", stats())