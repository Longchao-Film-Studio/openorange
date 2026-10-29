#!/usr/bin/env python3
"""数字 UID 身份模型测试（INV-UID）。

用法:
    python tests/test_uid_identity.py

被测的不变量（docs/身份与UID模型.md）：

    INV-UID-1  数字 UID 由 accounts.id 派生，是双射 —— 不同账号永不撞号
    INV-UID-2  拿不到 id 就报错，不许兜底成常数
    INV-UID-3  数字 UID 只出现在 fp-passport 协议里，不许当 key（调用点白名单）
    INV-UID-4  数据库里只有 username / account_id 两个身份，不许再有 fp_uid / channel_uid 列
    INV-UID-5  网页「游戏内 UID」和游戏里 `UID: xxx` 是同一个值（username）

每条都能红：把实现改回「哈希后 mod 9e8」→ INV-UID-1/2 红；
把 channel_uid 兜底加回去 → INV-UID-6 红；把网页 uid 改回数字 → INV-UID-5 红。
"""
import ast
import os
import sqlite3
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ 必须在 import shared.db 之前把库指到临时目录，否则会动真实 shared/data.db
_TMP = tempfile.mkdtemp(prefix="oo_uid_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "uid.db")

from shared import db, models  # noqa: E402

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


# ---------------------------------------------------------------- INV-UID-1
def test_bijection():
    print("\n[1] INV-UID-1 双射：20 万个账号不能撞号")
    n = 200_000
    seen = {}
    dup = None
    for i in range(1, n + 1):
        v = models._to_uid_number({"id": i})
        if v in seen:
            dup = (seen[v], i, v)
            break
        seen[v] = i
    check(f"{n} 个账号全部不同号", dup is None, f"撞号: {dup}")
    check("基准固定", models._to_uid_number({"id": 1}) == models.UID_NUM_BASE + 1,
          models._to_uid_number({"id": 1}))
    check("严格递增 1",
          models._to_uid_number({"id": 7}) - models._to_uid_number({"id": 6}) == 1,
          models._to_uid_number({"id": 7}))


# ---------------------------------------------------------------- INV-UID-2
def test_no_fallback():
    print("\n[2] INV-UID-2 缺 id 必须报错，不许兜底")
    for bad in ({}, {"username": "x"}, {"account_id": "oo_x"}, {"fp_uid": "123"}):
        try:
            v = models._to_uid_number(bad)
        except ValueError:
            check(f"{bad} 抛 ValueError", True)
        else:
            check(f"{bad} 抛 ValueError", False, f"返回了 {v}（兜底又回来了）")
    check("已删除 fp_uid_for", not hasattr(models, "fp_uid_for"))


# ---------------------------------------------------------------- INV-UID-4（迁移）
def test_migration():
    print("\n[3a] INV-UID-4 老库迁移：带 fp_uid/channel_uid 的库要被清成两列身份")
    legacy = os.path.join(_TMP, "legacy.db")
    if os.path.exists(legacy):
        os.remove(legacy)
    c = sqlite3.connect(legacy)
    c.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL UNIQUE,
            fp_uid TEXT NOT NULL,
            session_key TEXT NOT NULL,
            channel_uid TEXT,
            is_formal INTEGER NOT NULL DEFAULT 1,
            is_new INTEGER NOT NULL DEFAULT 1,
            created_ts INTEGER NOT NULL,
            login_count INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO accounts(username,account_id,fp_uid,session_key,channel_uid,created_ts)
        VALUES('legacy_a','oo_legacy','fp_legacy','sk',  'legacy_a', 1);
        INSERT INTO accounts(username,account_id,fp_uid,session_key,channel_uid,created_ts)
        VALUES('legacy_b','oo_legacy2','fp_legacy2','sk2','legacy_b',2);
    """)
    c.commit()
    c.close()

    # 把 db 指到老库并重连（模块内连接是 thread-local 缓存）
    saved = db.DB_PATH
    with __import__("contextlib").suppress(Exception):
        db._local.conn.close()          # noqa: SLF001
    db._local.conn = None               # noqa: SLF001
    db.DB_PATH = legacy
    try:
        db.init()
        cols = {r[1] for r in sqlite3.connect(legacy).execute("PRAGMA table_info(accounts)")}
        check("迁移后没有 fp_uid 列", "fp_uid" not in cols, sorted(cols))
        check("迁移后没有 channel_uid 列", "channel_uid" not in cols, sorted(cols))
        rows = list(sqlite3.connect(legacy).execute("SELECT username,account_id FROM accounts ORDER BY id"))
        check("老账号数据保住了", rows == [("legacy_a", "oo_legacy"), ("legacy_b", "oo_legacy2")], rows)
    finally:
        with __import__("contextlib").suppress(Exception):
            db._local.conn.close()      # noqa: SLF001
        db._local.conn = None           # noqa: SLF001
        db.DB_PATH = saved


# ---------------------------------------------------------------- INV-UID-4
def test_schema():
    print("\n[3] INV-UID-4 账户表只有两个身份列")
    db.init()
    cols = {r[1] for r in sqlite3.connect(db.DB_PATH).execute("PRAGMA table_info(accounts)")}
    check("没有 fp_uid 列", "fp_uid" not in cols, sorted(cols))
    check("没有 channel_uid 列", "channel_uid" not in cols, sorted(cols))
    check("有 username", "username" in cols)
    check("有 account_id", "account_id" in cols)
    check("有 id", "id" in cols)


# ---------------------------------------------------------------- 协议字段
def test_ticket_and_login():
    print("\n[4] 协议字段：数字只走 fp-passport")
    u = "uid_case_01"
    acc = db.register_account(u, "pw123456", models.account_id_for(u), models.session_key_for(u))
    check("注册成功", acc is not None)

    try:
        models.ticket_for(acc["account_id"])          # noqa: 故意少传一个参数
    except TypeError:
        check("ticket_for 缺 uid_num 直接 TypeError（没有兜底）", True)
    else:
        check("ticket_for 缺 uid_num 直接 TypeError（没有兜底）", False, "居然能调通")

    tk = models.ticket_for(acc["account_id"], models._to_uid_number(acc))
    check("ticket 里 channel_uid 是纯数字串",
          isinstance(_meta(tk).get("channel_uid"), str) and _meta(tk)["channel_uid"].isdigit(),
          _meta(tk).get("channel_uid"))

    ret, err = models.login(u)          # login(username, _channel_uid=None)，不吃密码
    check("登录成功", not err and isinstance(ret, dict), err)
    if isinstance(ret, dict):
        num = models._to_uid_number(acc)
        check("fp_uid 是 int", isinstance(ret.get("fp_uid"), int), type(ret.get("fp_uid")))
        check("fpid 是 int", isinstance(ret.get("fpid"), int), type(ret.get("fpid")))
        check("fp_uid == fpid", ret.get("fp_uid") == ret.get("fpid"))
        check("fp_uid == base + accounts.id", ret.get("fp_uid") == num, ret.get("fp_uid"))
        check("channel_uid 是数字串", str(ret.get("channel_uid")).isdigit(), ret.get("channel_uid"))
        check("channel_uid == str(fp_uid)", ret.get("channel_uid") == str(num))


def _meta(ticket_b64):
    import base64
    import json
    raw = base64.b64decode(ticket_b64).decode("utf-8", "replace")
    i = raw.find("{")
    return json.loads(raw[i:]) if i >= 0 else {}


# ---------------------------------------------------------------- INV-UID-5
def test_profile():
    print("\n[5] INV-UID-5 网页的「游戏内 UID」必须等于 username")
    u = "uid_case_02"
    db.register_account(u, "pw123456", models.account_id_for(u), models.session_key_for(u))
    acc = db.get_account_by_username(u)
    prof = models.account_profile(acc)
    check("profile.uid == username", prof.get("uid") == u, prof.get("uid"))
    check("profile 里不再有数字 uid 字段",
          not any(k in prof for k in ("uid_num", "fp_uid", "fpid")), sorted(prof))
    check("profile.account_id 保留", prof.get("account_id") == acc["account_id"])


# ---------------------------------------------------------------- INV-UID-3
def test_call_site_whitelist():
    print("\n[6] INV-UID-3 数字 UID 只能当协议值，不许当 key")

    def calls_in(path):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_to_uid_number":
                out.append(node.lineno)
        return out

    bad = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".mypy_cache", ".ruff_cache",
                                    "tests", "work", "node_modules", "Python")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                lines = calls_in(path)
            except SyntaxError:
                continue
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if rel != "shared/models.py" and lines:
                bad.append((rel, lines))
    check("只有 shared/models.py 调用它", bad == [], bad)

    mine = calls_in(os.path.join(ROOT, "shared", "models.py"))
    check("models.py 里只有 1 处调用（login）", len(mine) == 1, mine)


def main():
    print("tmp db =", db.DB_PATH)
    test_bijection()
    test_no_fallback()
    test_migration()
    test_schema()
    test_ticket_and_login()
    test_profile()
    test_call_site_whitelist()

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
