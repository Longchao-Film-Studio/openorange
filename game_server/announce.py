#!/usr/bin/env python3
"""全服公告 / 喊话（服务端唯一入口）。

三条 S2C 通道（契约见 docs/S2C接收端地图.md，索引取自 work/rpc_list.tsv）：

    kind="banner"  sendWorldBanner   882562061  ClientAvatar     全服跑马灯（横向滚动）
    kind="toast"   RpcPopToastText   696796776  NotifyComp       普通 Toast（原文显示）
    kind="gm"      GmRpcOnNotifyTip  1327864543 AvatarDebugComp  官方 crontab 提示通道

★ 为什么 banner 的长度上限比 toast 小得多
  跑马灯的滚动时长由客户端按文本宽度现算：
      duration = (maskWidth + textWidth) / 80 秒      （node_marqueegroup.lua:54）
  中文按 ~24px/字估算，60 字 ≈ 18 秒 —— 已经长到影响观感；200 字要滚一分钟，
  等于把玩家按在屏幕上看广告。所以：
      banner <= 80 字（约 24 秒）
      toast/gm <= 200 字（Toast 停留时间由客户端定，与长度无关）

★ 目标
  uid 为空 = 全服广播（只发已登录连接，见 online.broadcast 的 real_only）
  uid 非空 = 只发这个账号的所有连接（online.send_to）

★ 历史
  内存环形缓冲（最近 50 条），只用于管理页展示；不落库 —— 公告是瞬时操作，
  重启丢历史可以接受，也不需要参与存档一致性。
"""
import threading
import time

from shared.log_tee import get_logger

from . import ms as msmod
from .transport import online

_log = get_logger(__name__)

# kind -> (S2C 方法名, genRpcIndex 索引, 单条上限字数)
KINDS = {
    "banner": ("sendWorldBanner", 882562061, 80),
    "toast": ("RpcPopToastText", 696796776, 200),
    "gm": ("GmRpcOnNotifyTip", 1327864543, 200),
}
MAX_HISTORY = 50

_LOCK = threading.Lock()
_HISTORY = []          # 最近的公告记录（新的在后）


def build_frame(kind, text):
    """按 kind 造一条 S2C 实体消息帧。"""
    name, index, _limit = KINDS[kind]
    return msmod.build_entity_message(
        entity_id="", method_name=name, index=index,
        params=msmod.mp_arr([text]))


def announce(text, kind="banner", uid=None):
    """发一条公告。返回结果 dict（ok / kind / text / targets / ok_count / fail_count / error）。

    不抛异常：参数错误也走返回值，调用方（HTTP / 指令）直接回给用户。
    ★ 所有路径（含参数校验失败）都进历史 —— 公告是有审计价值的动作，
      "谁在什么时候想发什么、为什么没发出去"要能回看。
    """
    res = _do_announce(text, kind, uid)
    _remember(res)
    if res.get("ok"):
        _log.info(f"[announce] kind={res.get('kind')} uid={res.get('uid') or '<全服>'} "
                  f"targets={res.get('targets')} ok={res.get('ok_count')} "
                  f"fail={res.get('fail_count')} text={res.get('text')!r}")
    else:
        _log.warning(f"[announce] 失败：{res.get('error')} text={text!r}")
    return res


def _do_announce(text, kind="banner", uid=None):
    """真正干活的部分（不记账、不打日志），由 announce() 包一层。"""
    kind = (kind or "banner").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "error": f"未知类型 {kind!r}，可选：{', '.join(KINDS)}"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "文案不能为空"}

    _name, _index, limit = KINDS[kind]
    if len(text) > limit:
        return {"ok": False, "error": f"{kind} 最多 {limit} 字（当前 {len(text)}）"}

    uid = (str(uid).strip() or None) if uid is not None else None
    pkt = build_frame(kind, text)

    if uid:
        ok_n, fail_n = online.send_to(uid, [pkt])
        targets = ok_n + fail_n
        if targets == 0:
            res = {"ok": False,
                   "error": f"账号 {uid} 当前不在线（没有任何连接）",
                   "kind": kind, "text": text, "uid": uid,
                   "targets": 0, "ok_count": 0, "fail_count": 0}
            return res
    else:
        stat = online.broadcast([pkt])
        targets = stat["targets"]
        ok_n, fail_n = stat["ok"], stat["fail"]
        if targets == 0:
            res = {"ok": False, "error": "当前没有在线玩家（公告没发出去）",
                   "kind": kind, "text": text, "uid": None,
                   "targets": 0, "ok_count": 0, "fail_count": 0}
            return res

    return {"ok": True, "kind": kind, "text": text, "uid": uid,
            "targets": targets, "ok_count": ok_n, "fail_count": fail_n}


def _remember(res):
    """记入环形历史（内存，最近 50 条）。"""
    entry = {
        "ts": int(time.time()),
        "kind": res.get("kind") or "",
        "text": res.get("text") or "",
        "uid": res.get("uid"),
        "ok": bool(res.get("ok")),
        "error": res.get("error") or "",
        "targets": res.get("targets") or 0,
        "ok_count": res.get("ok_count") or 0,
        "fail_count": res.get("fail_count") or 0,
    }
    with _LOCK:
        _HISTORY.append(entry)
        if len(_HISTORY) > MAX_HISTORY:
            del _HISTORY[: len(_HISTORY) - MAX_HISTORY]


def history(limit=20):
    """最近的公告记录（新的在前）。"""
    with _LOCK:
        items = list(_HISTORY)
    items.reverse()
    return items[: max(0, int(limit))]


def kinds():
    """给管理页用的类型清单。"""
    return [{"kind": k, "method": v[0], "index": v[1], "limit": v[2]}
            for k, v in KINDS.items()]


def reset_for_tests():
    with _LOCK:
        _HISTORY.clear()
