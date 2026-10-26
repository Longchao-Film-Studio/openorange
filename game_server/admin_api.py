#!/usr/bin/env python3
"""game_server.admin_api —— 游戏服的本地管理接口（给网页命令系统用）。

为什么要有这个
--------------
网页端（8399）和游戏服（33100）是两个进程。命令要改游戏服的行为，
需要一个**显式的服务边界**，而不是两边共用一个 SQLite 表互相猜。

设计
----
- 只监听 127.0.0.1（不对外）
- 需要 X-OO-Token 头（OO_ADMIN_TOKEN -> work/admin_token.txt -> 自动生成）
- 同步返回结果，命令能立刻知道成功与否
- 直接改游戏服**内存状态**并落盘，不是「等下次登录生效」

接口
----
    GET  /health
    GET  /state
    POST /dungeon/level            {"level": 50003}   0 = 取消覆盖
    POST /player/skip-prologue     {"uid": "xxx"}
    GET  /player/{uid}/story       看某账号剧情状态
"""
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from shared import admin_token as _admin_token
from shared.log_tee import get_logger

from . import runtime

_log = get_logger(__name__)

DEFAULT_PORT = 33102


def _token():
    """管理接口口令（雷3）。

    优先级：OO_ADMIN_TOKEN -> work/admin_token.txt -> 自动生成并落盘。
    ★ 不再有 "local-dev" 这类公开默认值 —— 同机任何进程都能用它发物品。
    """
    return _admin_token.get_token()


def port():
    try:
        return int(os.environ.get("OO_ADMIN_PORT") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


# ---------------------------------------------------------------- 业务实现


def _do_unlock_npc_topic(uid, topic_id):
    """管理命令 /sms 的写入端：给账号解锁一条短信话题（GM 验证用）。

    与 /player/skip-prologue 同样的前置：先保证状态完整，再走领域层唯一入口。
    allow_unmapped=True —— 这是显式 GM 越权，与任务路径（默认 False）区分。
    """
    from . import ms as msmod
    from . import player_state
    from .story import npc_sms
    st = player_state.peek(uid)
    if st is None or not isinstance(st.props.get("story_task_bag"), dict):
        player_state.reset_state(
            msmod.conv_int_keys(
                msmod.AVATAR_INIT_DICT.get("__Properties__") or {}),
            uid=uid)
        player_state.peek(uid).is_new = True
    msmod.live_init_dict(uid)
    st = player_state.get_state(uid=uid)
    cfg = __import__("game_server.gamedata", fromlist=["gamedata"]).npc_message(topic_id)
    if not isinstance(cfg, dict):
        return {"ok": False, "error": f"话题 {topic_id} 不在 NpcMessageData"}
    try:
        chat_id = int(cfg.get("role"))
    except Exception:
        return {"ok": False, "error": f"话题 {topic_id} 缺 role 字段"}
    frames = npc_sms.unlock_topic(st, chat_id, topic_id,
                                  source="admin:/sms", allow_unmapped=True)
    return {"ok": True, "chat_id": chat_id, "frames": len(frames),
            "chat_name": cfg.get("chatName")}
def _do_skip_prologue(uid):
    """立刻把某个账号的序章标完成（不等下次登录）。

    必须先走一遍 `ms.live_init_dict(uid)`：
      - 新号第一次被创建时，它会应用脚手架/新号剧情起点（任务 1000 设为进行中）
      - 直接 get_state 拿到的是**空白状态**，story_task_bag 还没有 1000，
        后面 finish_story_task 就找不到任务（实测 story_task_1000 返回 -1）。
    """
    from . import ms as msmod
    from . import player_state, story
    # 没有存档 / 存档是空壳（被查询命令创建过）-> 用完整初始数据重建
    st = player_state.peek(uid)
    if st is None or not isinstance(st.props.get("story_task_bag"), dict):
        player_state.reset_state(
            msmod.conv_int_keys(
                msmod.AVATAR_INIT_DICT.get("__Properties__") or {}),
            uid=uid)
        player_state.peek(uid).is_new = True
    msmod.live_init_dict(uid)          # 走一遍新号初始化（任务 1000 设为进行中）
    st = player_state.get_state(uid=uid)
    done_before = story.story_state(st.props, story.FIRST_STORY_TASK)
    if done_before != story.TASK_STATE_FINISHED:
        # ★ 审计 P1-2：不能直接调 story.finish_story_task —— 那会绕过发奖、
        #   属性同步和 _recalc_story_unlocks，和「落状态唯一入口」的声明矛盾。
        #   走同一条主路径，返回的帧这里没人可推（GM 命令不带连接），直接丢弃。
        from .story.rpc import finish_story_tasks
        finish_story_tasks(st, [story.FIRST_STORY_TASK])
    sf = st.props.get("story_finished")
    if isinstance(sf, dict):
        sf[1] = True
        st.set("story_finished", sf)
    st.save()
    return {
        "uid": uid,
        "already_finished": done_before == story.TASK_STATE_FINISHED,
        "story_task_1000": story.story_state(st.props, story.FIRST_STORY_TASK),
        "story_finished_1": bool(isinstance(sf, dict) and sf.get(1) is True),
        "story_task_bag": sorted([k for k in (st.props.get("story_task_bag") or {})
                                  if isinstance(k, int)]),
    }


def _player_story(uid):
    from . import player_state, story
    st = player_state.peek(uid)      # 查询不创建存档
    if st is None:
        return {"uid": uid, "exists": False, "story_task_1000": -1,
                "story_finished_1": False, "active": [],
                "level": None, "street_id": None}
    bag = st.props.get("story_task_bag") or {}
    sf = st.props.get("story_finished") or {}
    return {
        "uid": uid,
        "story_task_1000": story.story_state(st.props, story.FIRST_STORY_TASK),
        "story_finished_1": bool(isinstance(sf, dict) and sf.get(1) is True),
        "active": sorted([k for k in bag if isinstance(k, int)]),
        "level": st.props.get("level"),
        "street_id": st.props.get("street_id"),
    }


def _player_dungeon(uid):
    """查某个账号的副本关卡覆盖。"""
    from . import player_state
    st = player_state.peek(uid)
    if st is None:
        return {"uid": uid, "exists": False, "force_level": 0}
    try:
        lv = int(st.props.get("_oo_dungeon_force_level") or 0)
    except (TypeError, ValueError):
        lv = 0
    return {"uid": uid, "exists": True, "force_level": lv}


def _set_player_force_level(uid, level):
    """设置某个账号的副本关卡覆盖。level=0 表示取消。

    ★ 按账号存，不是全局。存在玩家存档的 _oo_ 私有字段里：
      - _oo_ 前缀在 ms.live_init_dict 里会被过滤，客户端看不到
      - 重启游戏服不丢
      - 多开/换号互不影响
    """
    from . import ms as msmod
    from . import player_state
    try:
        v = int(level or 0)
    except (TypeError, ValueError):
        v = 0
    v = v if v > 0 else 0
    # 没有存档就先建一份完整的（别留空壳，否则会毁掉新号初始化）
    st = player_state.peek(uid)
    if st is None or not isinstance(st.props.get("story_task_bag"), dict):
        player_state.reset_state(
            msmod.conv_int_keys(
                msmod.AVATAR_INIT_DICT.get("__Properties__") or {}),
            uid=uid)
        player_state.peek(uid).is_new = True
        msmod.live_init_dict(uid)
    st = player_state.get_state(uid=uid)
    if v:
        st.props["_oo_dungeon_force_level"] = v
    else:
        st.props.pop("_oo_dungeon_force_level", None)
    st.save()
    _log.info("[admin] {} 的副本关卡覆盖设为 {}".format(uid, v or "无"))
    return v


def _ensure_player_state(uid):
    """确保某账号有完整存档（别留空壳）。返回 (st, ok)。"""
    from . import ms as msmod
    from . import player_state
    st = player_state.peek(uid)
    if st is None or not isinstance(st.props.get("story_task_bag"), dict):
        player_state.reset_state(
            msmod.conv_int_keys(
                msmod.AVATAR_INIT_DICT.get("__Properties__") or {}),
            uid=uid)
        player_state.peek(uid).is_new = True
        msmod.live_init_dict(uid)
    return player_state.get_state(uid=uid)


def _grant_player(uid, items=None, level=None, level_add=None):
    """GM 命令的真写入入口：发物品 / 设等级。

    ★ 诚实化（2026-10-23）：/give /lv /money 不再「解析+预览+未生效」，
      走这里真改玩家存档，返回实际结果。

    items: {item_id_str: count}   —— 与结算发奖同一入口 story.add_item
    level: 直接设为该等级（>=1）
    level_add: 在现有等级上加（>=1）
    """
    st = _ensure_player_state(uid)
    granted = []
    if items:
        items_map = {}
        for k, v in (items or {}).items():
            try:
                iid = int(k)
                cnt = int(v)
            except (TypeError, ValueError):
                continue
            if cnt > 0:
                items_map[iid] = items_map.get(iid, 0) + cnt
        if items_map:
            # ★ 2026-10-26：管理接口也走唯一发放入口（堆叠/maxNum/ext/autoUse 全在 grants）
            from .property.bag import grants as _bag_grants
            _tx, outcomes = _bag_grants.grant_many(st.props, items_map, source="admin")
            for oc in outcomes:
                if oc.ok:
                    key = oc.grids[-1] if oc.grids else ""
                    grid = (st.props.get("item_bag") or {}).get(key) if key else None
                    now = int(grid.get("num") or 0) if isinstance(grid, dict) else 0
                    granted.append({"item_id": oc.item_id, "count": oc.count,
                                    "now": now})
                else:
                    granted.append({"item_id": oc.item_id, "count": oc.count,
                                    "error": str(oc.error)})
    if level:
        try:
            lv = int(level)
        except (TypeError, ValueError):
            lv = 0
        if lv >= 1:
            st.props["level"] = lv
            granted.append({"level": lv})
    if level_add:
        try:
            add = int(level_add)
        except (TypeError, ValueError):
            add = 0
        if add >= 1:
            cur = int(st.props.get("level") or 1)
            st.props["level"] = cur + add
            granted.append({"level": cur + add})
    st.save()
    return {"uid": uid, "granted": granted}


# ---------------------------------------------------------------- HTTP 层
# ---------------------------------------------------------------- HTTP 层
class _Handler(BaseHTTPRequestHandler):
    server_version = "OOAdmin/1.0"

    def log_message(self, fmt, *args):  # 静音，别刷屏
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        tok = self.headers.get("X-OO-Token") or ""
        if tok != _token():
            self._send(403, {"ok": False, "error": "bad token"})
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if not self._auth():
            return None
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/health":
                return self._send(200, {"ok": True,
                                        "started_at": runtime.STARTED_AT})
            if path == "/announce":          # 公告历史（管理页用）
                from . import announce as _announce
                return self._send(200, {"ok": True,
                                        "kinds": _announce.kinds(),
                                        "history": _announce.history(30)})
            if path == "/state":
                from . import config as ooconf
                state = runtime.snapshot()
                # 命令行 --dungeon-last-only 的开关在 config 里（不是 runtime），
                # 之前 /state 报的是 runtime 那份没人写的镜像，恒为 False。
                state["cli_dungeon_last_only"] = bool(
                    ooconf.DUNGEON_LAST_ONLY)
                return self._send(200, {"ok": True, "state": state})
            if path.startswith("/player/") and path.endswith("/dungeon"):
                uid = path[len("/player/"):-len("/dungeon")]
                return self._send(200, {"ok": True,
                                        "player": _player_dungeon(uid)})
            if path.startswith("/player/") and path.endswith("/story"):
                uid = path[len("/player/"):-len("/story")]
                return self._send(200, {"ok": True,
                                        "player": _player_story(uid)})
            return self._send(404, {"ok": False, "error": "no such path"})
        except Exception as e:
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "trace": traceback.format_exc()[-800:]})

    _ROUTES: ClassVar[dict[str, str]] = {
        "/announce": "_r_announce",
        "/dungeon/level": "_r_dungeon_level",
        "/player/unlock-npc-topic": "_r_player_unlock_npc_topic",
        "/player/skip-prologue": "_r_player_skip_prologue",
        "/player/grant": "_r_player_grant",
        "/player/level": "_r_player_level",
    }

    def do_POST(self):
        """POST 路由分发（2026-10-25 拆分：每个端点一个方法）。

        等价性：auth / path 归一化 / payload 解析 / 404 / 500 兜底全部保持不变；
        每个方法体与原 if 体逐行一致。异常仍由本函数的 except 统一兜住。
        """
        if not self._auth():
            return None
        path = self.path.split("?")[0].rstrip("/") or "/"
        payload = self._body()
        try:
            name = self._ROUTES.get(path)
            if name is None:
                return self._send(404, {"ok": False, "error": "no such path"})
            return getattr(self, name)(payload)
        except Exception as e:
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "trace": traceback.format_exc()[-800:]})

    def _r_announce(self, payload):
        """发全服公告 / 定向喊话。

        payload: {kind: "banner"|"toast"|"gm", text: str, uid?: str}
        uid 为空 = 全服广播（只发已登录连接）；非空 = 只发该账号所有连接。
        """
        from . import announce as _announce
        res = _announce.announce(
            str(payload.get("text") or ""),
            kind=str(payload.get("kind") or "banner"),
            uid=(payload.get("uid") or None))
        return self._send(200 if res.get("ok") else 400, res)

    def _r_dungeon_level(self, payload):
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return self._send(400, {"ok": False, "error": "missing uid"})
        from . import player_state as _pstate
        with _pstate.lock_for(uid):
            lv = _set_player_force_level(uid, payload.get("level"))
        return self._send(200, {"ok": True, "uid": uid,
                                 "force_level": lv})

    def _r_player_unlock_npc_topic(self, payload):
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return self._send(400, {"ok": False, "error": "missing uid"})
        try:
            topic_id = int(payload.get("topic_id"))
        except Exception:
            return self._send(400, {"ok": False, "error": "topic_id 非法"})
        from . import player_state as _pstate
        with _pstate.lock_for(uid):
            res = _do_unlock_npc_topic(uid, topic_id)
        if not res.get("ok"):
            return self._send(400, res)
        return self._send(200, {"ok": True, "result": res})

    def _r_player_skip_prologue(self, payload):
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return self._send(400, {"ok": False, "error": "missing uid"})
        from . import player_state as _pstate
        with _pstate.lock_for(uid):
            res = _do_skip_prologue(uid)
        return self._send(200, {"ok": True, "result": res})

    def _r_player_grant(self, payload):
        # ★ GM 命令诚实化：/give /money 的真写入入口
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return self._send(400, {"ok": False, "error": "missing uid"})
        items = payload.get("items")
        if not isinstance(items, dict):
            return self._send(400, {"ok": False, "error": "items 需是 {itemId: 数量}"})
        from . import player_state as _pstate
        with _pstate.lock_for(uid):
            res = _grant_player(uid, items=items)
        return self._send(200, {"ok": True, "result": res})

    def _r_player_level(self, payload):
        # ★ GM 命令诚实化：/lv 的真写入入口
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return self._send(400, {"ok": False, "error": "missing uid"})
        lv = payload.get("level")
        try:
            lv = int(lv) if lv is not None else None
        except (TypeError, ValueError):
            return self._send(400, {"ok": False, "error": "level 需是整数"})
        if lv is None or lv < 1:
            return self._send(400, {"ok": False, "error": "level 需 >= 1"})
        from . import player_state as _pstate
        with _pstate.lock_for(uid):
            res = _grant_player(uid, level=lv)
        return self._send(200, {"ok": True, "result": res})


    def do_PUT(self):
        self.do_POST()


def _resolve_port(explicit):
    """解析监听端口。

    ★ 为什么单独成函数：start(port=...) 的参数名会遮蔽模块级 port()，
      写成 `p = port or port()` 就是 `None or None()` -> TypeError
      （D5 把 _port 重命名成 port 时就踩了这个坑，管理接口在真实启动路径上
      静默起不来；测试因为都显式传 port= 所以全绿，没拦住）。
      这里放一个不接收同名参数的函数，让 port() 指向模块级那个。

    explicit 为 0 时也要尊重（0 = 让内核分配临时端口，测试用）。
    """
    if explicit is not None:
        return int(explicit)
    return port()


def start(host="127.0.0.1", port=None):
    """在后台线程起管理接口。返回 (server, thread)。

    port=None 时取 OO_ADMIN_PORT / DEFAULT_PORT；port=0 表示临时端口。
    """
    if runtime.STARTED_AT is None:
        runtime.STARTED_AT = int(time.time())
    p = _resolve_port(port)
    srv = ThreadingHTTPServer((host, p), _Handler)
    th = threading.Thread(target=srv.serve_forever, name="oo-admin-api",
                          daemon=True)
    th.start()
    return srv, th
