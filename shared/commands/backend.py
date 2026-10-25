"""游戏服管理接口的 HTTP 客户端 + 「后端未接入」提示。

网页端（8399）和游戏服（33100）是两个进程。命令要改游戏服状态，走游戏服的
本地 HTTP 管理接口（game_server/admin_api.py），不共用数据库表 ——
显式边界、同步拿结果、能返回真实状态。

★ ADMIN_URL / ADMIN_TOKEN 每次调用时从包命名空间取（而不是本模块全局），
  这样 tests 里 cmds.ADMIN_URL = ... 的 monkeypatch 依然生效。
"""
import os

BACKEND_HINT = "游戏端 RPC 尚未接入，本次只解析校验、未实际生效"

ADMIN_URL = os.environ.get("OO_ADMIN_URL") or "http://127.0.0.1:33102"
# ★ 雷3：不再有 "local-dev" 默认值。None 表示「调用时按
#   OO_ADMIN_TOKEN -> work/admin_token.txt -> 自动生成」解析。
#   测试仍可以 monkeypatch cmds.ADMIN_TOKEN 覆盖。
ADMIN_TOKEN = os.environ.get("OO_ADMIN_TOKEN") or None


def _pkg():
    """取包命名空间（延迟 import，避免循环依赖）。"""
    from .. import commands
    return commands


def admin_call(method, path, payload=None):
    """调游戏服管理接口。返回 (json, err)。"""
    import json as _json
    import urllib.error
    import urllib.request
    pkg = _pkg()
    url = (pkg.ADMIN_URL or "").rstrip("/") + path
    data = None
    if payload is not None:
        data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    token = pkg.ADMIN_TOKEN
    if not token:
        from .. import admin_token as _at
        token = _at.get_token()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-OO-Token": token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return _json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            return _json.loads(e.read().decode("utf-8")), None
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def admin_down():
    """连不上管理接口时的提示文本（URL 每次现取，别缓存）。"""
    return (f"连不上游戏服管理接口（{_pkg().ADMIN_URL}）。\n"
            "  游戏服没在运行？先启动 game_server.server --ms-ok。")
