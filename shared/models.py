#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared.models —— 账号/票据的业务逻辑(认证服与游戏服共用)

账号策略(已改为网页注册制):
  - **游戏内不再自动建号**。账号只能通过网页端注册(手机号 + 密码)。
  - 手机号 = UID(不验证真实性, 只校验格式)
  - 游戏内 send_code 前必须: ①账号已注册 ②网页会话有效
  - login 只对已注册账号签发 ticket
"""
import hashlib
import json
import re
import time

from . import db

# 手机号只校验"像不像手机号", 不验证真实性
MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

# 默认国家码(FPX.dll loginret.hpp 的 csc 字段)
DEFAULT_CSC = "86"

# ---- 渠道标识(取自官方真实 ticket 的 JSON 元数据) ----
# 官方样本: "channel_id":"fp_account_cn_pc","sub_channel_id":"official",
#           "package_id":"fp_account_cn_pc.official.Amukpj"
APP_ID         = "deep.cn.prod"
CHANNEL_ID_PC  = "fp_account_cn_pc"
SUB_CHANNEL    = "official"
PACKAGE_ID     = "fp_account_cn_pc.official.Amukpj"


def is_valid_mobile(mobile):
    """格式校验: 中国大陆手机号样式(1[3-9] 开头 11 位)。"""
    return bool(MOBILE_RE.match(str(mobile or "")))


def _mask_mobile(mobile):
    """手机号打码, 与官方一致: 18***70 -> 前2位 + *** + 后2位。"""
    s = str(mobile or "")
    if len(s) < 5:
        return s
    return s[:2] + "***" + s[-2:]


def _to_uid_number(acc):
    """把账号转成官方那样的 **纯数字** UID。

    官方 fp_uid = 35931389584 (int)。
    我们的账号表存的是字符串, 这里用稳定哈希映射到数字区间, 保证同一账号恒定。
    """
    raw = acc.get("fp_uid") or acc.get("account_id") or acc.get("mobile") or ""
    raw = str(raw)
    if raw.isdigit():
        return int(raw)
    h = hashlib.sha256(("oo_uid:" + raw).encode("utf-8")).hexdigest()
    # 取 9 位十进制(1e8 ~ 1e9), 与官方的 3.5e10 同量级
    return 100000000 + (int(h[:16], 16) % 900000000)


def _stable_hex(seed, n=32):
    """由种子生成稳定的十六进制串(同一手机号永远得到同一账号)。"""
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    while len(h) < n:
        h += hashlib.sha256(h.encode()).hexdigest()
    return h[:n]


def account_id_for(mobile):
    return "oo_" + _stable_hex("acct:" + mobile, 16)


def fp_uid_for(mobile):
    return "fp_" + _stable_hex("uid:" + mobile, 16)


def _b64e(raw):
    """base64(无换行)。"""
    import base64
    return base64.b64encode(raw).decode("ascii")


def _b64d(s):
    import base64
    s = str(s or "").strip()
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


# --------------------------------------------------------------------------
# session_key / ticket —— 结构与官方**逐字节一致**
#
# 官方真实样本(来源: GuiLongchao/prefs/deep_cn_setting.ini -> cache_account_infos)
#
# session_key = base64( 8hex | app_id | account_id | device_id | t1 | t2 | 32hex )
#   解码后 127 字节明文:
#     00e424c8|deep.cn.prod|fp_account_cn:1869080|
#     a399f2983a893cbdca576606b89fe892|69388f74|6945be74|
#     856c4536b55fe70a6aef91b7ad039747
#   分隔符 = '|', 共 7 段
#
# ticket = base64( 64hex 前缀 + JSON 元数据 )
#   解码后 375 字节: 前 64 字符是 hex, 紧跟 '{' 开始的 JSON:
#     {"app_id","channel_id","sub_channel_id","package_id","account_id",
#      "channel_uid","time","client_ip","ext","info":{"fpid_list",
#      "current_fpid","is_new"}}
#
# ★ 客户端(testsdk/FPXAccount)会 **先 base64 解码 ticket, 再取 '{' 之后的
#   JSON**; 若给的是纯 hex, 解不出 JSON -> 静默失败 -> 停在登录 UI。
#   这是"验证码被消费但界面无反应"的直接原因。
# --------------------------------------------------------------------------

def _hex(n):
    import os
    return os.urandom(n // 2).hex()


def session_key_for(mobile, account_id=None, device_id=None):
    """按官方结构生成 session_key(7 段, '|' 分隔, 整体 base64)。"""
    acc = db.get_account_by_mobile(mobile) or {}
    account_id = account_id or acc.get("account_id") or account_id_for(mobile)
    device_id = device_id or "a399f2983a893cbdca576606b89fe892"
    now = int(time.time())
    plain = "|".join([
        _hex(8),                       # 8 hex 随机前缀
        APP_ID,
        account_id,
        device_id,
        format(now & 0xFFFFFFFF, "08x"),
        format((now + 600) & 0xFFFFFFFF, "08x"),
        _hex(32),                      # 32 hex 签名
    ])
    return _b64e(plain.encode("utf-8"))


def ticket_for(mobile, account_id, channel_uid=None):
    """按官方结构生成 ticket(64hex 前缀 + JSON 元数据, 整体 base64)。"""
    acc = db.get_account_by_mobile(mobile) or {}
    uid = str(channel_uid or acc.get("channel_uid") or _to_uid_number(acc))
    meta = {
        "app_id": APP_ID,
        "channel_id": CHANNEL_ID_PC,
        "sub_channel_id": SUB_CHANNEL,
        "package_id": PACKAGE_ID,
        "account_id": account_id,
        "channel_uid": uid,
        "time": int(time.time()),
        "client_ip": "127.0.0.1",
        "ext": None,
        "info": {
            "fpid_list": [],
            "current_fpid": 0,
            "is_new": False,
        },
    }
    payload = _hex(64) + json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return _b64e(payload.encode("utf-8"))


# --------------------------------------------------------------------------
# 网页端: 注册 / 登录 / 登出
# --------------------------------------------------------------------------

def register(mobile, password):
    """网页注册。

    返回 (ok, msg, account_dict|None)。
    """
    mobile = str(mobile or "").strip()
    password = str(password or "")

    if not mobile:
        return False, "请输入手机号", None
    if not is_valid_mobile(mobile):
        return False, "手机号格式不正确(需 11 位, 1 开头)", None
    if len(password) < 6:
        return False, "密码至少 6 位", None
    if len(password) > 64:
        return False, "密码过长", None

    acc = db.register_account(
        mobile=mobile,
        password=password,
        account_id=account_id_for(mobile),
        fp_uid=fp_uid_for(mobile),
        session_key=session_key_for(mobile),
        channel_uid=mobile,
    )
    if acc is None:
        return False, "该手机号已注册", None
    return True, "注册成功", acc


def web_login(mobile, password):
    """网页登录。返回 (ok, msg, token|None)。"""
    mobile = str(mobile or "").strip()
    password = str(password or "")

    acc = db.get_account_by_mobile(mobile)
    if not acc or not acc.get("pass_hash"):
        return False, "账号不存在或未设置密码, 请先注册", None
    if not db.verify_password(password, acc.get("pass_salt"), acc.get("pass_hash")):
        return False, "密码错误", None

    token = db.create_session(mobile)
    db.touch_login(mobile)
    return True, "登录成功", token


def web_logout(token):
    db.drop_session(token)


def whoami(token):
    """由网页会话 token 取账号。"""
    s = db.get_session(token)
    if not s:
        return None
    return db.get_account_by_mobile(s["mobile"])


def can_request_code(mobile):
    """游戏内能否取验证码: 账号已注册 且 网页会话有效。

    返回 (ok: bool, reason: str)。
    """
    mobile = str(mobile or "").strip()
    if not mobile:
        return False, "mobile required"
    acc = db.get_account_by_mobile(mobile)
    if not acc:
        return False, "account not registered (please register on web first)"
    if not db.has_active_session(mobile):
        return False, "no web session (please login on web first)"
    return True, "ok"


# --------------------------------------------------------------------------
# 游戏端: 登录(只对已注册账号)
# --------------------------------------------------------------------------

def login(mobile, channel_uid=None):
    """游戏端登录: 账号必须已注册 -> 刷新登录态 + 签发票据。

    返回 (ret_dict, err_msg)。err_msg 非空表示失败。
    """
    mobile = str(mobile or "").strip()
    acc = db.get_account_by_mobile(mobile)
    if not acc:
        return None, "account not registered"

    fp_uid_num = _to_uid_number(acc)
    session_key = session_key_for(mobile, account_id=acc["account_id"])
    db.touch_login(mobile, session_key=session_key)

    ticket = ticket_for(mobile, acc["account_id"], channel_uid=fp_uid_num)
    db.issue_ticket(ticket, mobile, acc["account_id"])

    is_new = bool(acc["is_new"])
    if is_new:
        db.mark_not_new(mobile)

    # ---- 字段依据(FPX.dll 反射表, 两部分结构叠加) ----
    # loginret.hpp        : csc(*) / fp_uid(*) / qr_code_str / rail_order_id
    #                       / qr_cord_expire / payment_order_id
    #                       / bind_infos[](email,name,bind_display_name,bind_type,
    #                         session_key_timeout,bind_info,operation_ts,ctime,
    #                         display_bind_type,user_display_name)
    #                       (*) = 带 "json value result" 强解析, 缺了会报错
    # loginaccountret.hpp : fp_uid / session_key / is_new / fp_device_id / msg
    #                       / account_create_ts / channel_uid / account_id
    #                       / fpid_list / current_fpid / ticket
    #                       / delete_time_left / is_formal
    # ---- 官方真实响应样本(逐字段对照) ----
    # 来源: GuiLongchao/prefs/deep_cn_setting.ini -> cache_account_infos (base64)
    # 这是官方服务器成功登录后的响应缓存, 是唯一的权威类型依据。
    #
    # 字段            官方                 我们(修正前)        差异
    # -------------------------------------------------------------------
    # fp_uid         35931389584  int     "fp_bfc..." str    类型错
    # current_fpid   0            int     ""          str    类型错
    # fpid           35931389584  int     (缺失)             缺字段
    # bind_infos     "[{...}]"    str     [{...}]     list   类型错
    # account_create_ts 1726051819000     1789240656         毫秒/秒
    # is_formal      false                true               值不同
    # csc/msg/qr_*/rail_*/payment_*       (无)        有     多余字段
    #
    # ---- 官方响应字段全集(15 个, 逐字段核对于官方真实样本) ----
    #   account_create_ts 1726051819000     毫秒
    #   account_id        "fp_account_cn:1869080"
    #   bind_infos        "[{\"bind_type\":101,...}]"   字符串(内含 JSON)
    #   channel_uid       "35931389584"     字符串
    #   current_fpid      0                 int
    #   delete_time_left  0                 int
    #   fp_device_id      ""                字符串
    #   fp_uid            35931389584       int
    #   fpid              35931389584       int
    #   fpid_list         []                数组
    #   is_formal         false             bool
    #   is_new            false             bool
    #   session_key       base64(7段|分隔)   ★ 本轮修正
    #   ticket            base64(64hex+JSON) ★ 本轮修正
    #
    # 官方响应**没有** code/msg 字段 —— 成功响应就是这 15 个字段平铺。

    # bind_infos: 官方是 **JSON 字符串**, 不是数组
    bind_infos_obj = [
        {
            "bind_type": 101,
            "bind_display_name": _mask_mobile(mobile),
            "bind_info": {
                "id": _mask_mobile(mobile),
                "name": DEFAULT_CSC,
                "email": "",
            },
        }
    ]

    # ---- 官方响应字段全集: 15 个, 一个不多一个不少 ----
    return {
        "account_create_ts": int(acc["created_ts"]) * 1000,
        "account_id": acc["account_id"],
        "bind_infos": json.dumps(bind_infos_obj, ensure_ascii=False, separators=(",", ":")),
        "channel_uid": str(fp_uid_num),
        "current_fpid": 0,
        "delete_time_left": 0,
        "fp_device_id": "",
        "fp_uid": fp_uid_num,
        "fpid": fp_uid_num,
        "fpid_list": [],
        "is_formal": bool(acc["is_formal"]),
        "is_new": is_new,
        "session_key": session_key,
        "ticket": ticket,
    }, ""


def check_ticket(ticket):
    """游戏服校验票据用。"""
    t = db.get_ticket(ticket)
    if not t:
        return None
    if int(time.time()) > t["expire_ts"]:
        return None
    return t
