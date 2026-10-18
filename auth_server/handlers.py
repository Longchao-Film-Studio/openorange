#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth_server.handlers —— fp-passport 各接口实现

已确证接口(FPX_PROTOCOL_DICT.md):
  /api/get_config        启动拉配置(总开关: 决定后续请求去向)
  /api/send_code         获取短信验证码
  /api/send_code_session 会话取码
  /api/login             手机号+验证码登录
  /api/session_login     会话登录
  /api/auth_info /api/pre_id_auth /api/id_auth   实名防沉迷
  /api/ping              心跳
  /api/privacy/privacy   隐私
  /sdk/announce          公告
  /api/switch_bind /api/switch_check
  /api/unregister/cancel 注销
  /self_assist/get_token

所有接口: 请求体 = base64(AES-CBC(明文JSON)); 响应 = base64([随机IV]+AES-CBC(JSON))
"""
import hashlib
import json
import random
import time

from shared import db, models

from . import config

# --------------------------------------------------------------------------
# 客户端内置配置(必须与 StreamingAssets/fpx_config.json 完全一致!)
#
# 【重要】这些值是客户端**内置**的, FPX SDK 用它们做签名/校验。
# 如果 get_config 返回的值与内置值不同, 会导致签名不匹配 -> 登录流程中断。
# 来源: F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/StreamingAssets/fpx_config.json
# --------------------------------------------------------------------------
APP_ID       = "deep.cn.prod"
APP_SECRET   = "5217e02b-7fce-40fd-9c4c-55db1cb349d0"
CHANNEL_ID   = "mubao"
SUB_CHANNEL  = "mubao"
PACKAGE_ID   = "mubao.mubao.A6mfck"

# 两个域用**不同的** api_key(不是笔误, 内置配置就是这样)
AUTH_KEY     = "OkSHb2iUkYKrV9h6"     # auth 域(实名认证)
AUTH_SECRET  = APP_SECRET
FP_KEY       = "1wTOoUwJ4FxhIsoU"     # fp_account 域(用户中心) <- 登录走这个
FP_SECRET    = "5TB163SWDgXk8jvpHyGqMkVLoiOHN30x"
API_IV       = "1234567890abcdef"

GAME_CODE    = "openorange"
GAME_KEY     = "openorange"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _rand_code(n=6):
    return "".join(random.choice("0123456789") for _ in range(n))


# --------------------------------------------------------------------------
# /api/get_config —— 总开关
# --------------------------------------------------------------------------

def get_config(req):
    """返回客户端运行所需的全部配置。

    **字段依据: FPX.dll 的 config.hpp 字段表(逐字节挖出, 30 个字段)**
        privacy_baseurl, notice_baseurl, sobot_app_key, sobot_api_host,
        zcsobot_robot_code, zcsobot_robot_alias, secret, tag, sobot_system_num,
        sobot_channel_id, payHost, payProject, channel_1, channel_2, appId,
        api_secret, api_key, api_iv, funplusIap, ZMBiConfig, sobot,
        fpPassport, fpAccount, socialauth, gamePay,
        zcsobot_leave_template_id, zcsobot_deploy_id, funplusConfig, popups

    注意: 真实结构里 **没有 servers 字段**, 是 channel_1/channel_2/fpPassport/
    fpAccount 这种平铺命名。之前按"servers"写是错的。
    """
    base = "https://%s" % config.PUBLIC_HOST
    host = config.PUBLIC_HOST

    # ---- 通道配置(channel_1 / channel_2 同构) ----
    def _channel(idx):
        return {
            "payHost": base,
            "payProject": GAME_CODE,
            "appId": APP_ID,
            "api_secret": AUTH_SECRET,
            "api_key": FP_KEY,
            "api_iv": API_IV,
            "channel_name": CHANNEL_ID,
        }

    # ---- fpPassport: 账号服务主入口 ----
    fp_passport = {
        "env": config.ENV,
        "passport": host,
        "auth_baseurl": base,
        "authurl": base,
        "self_assist_url": base,
        "logoffurl": base,
        "domain": base,
        "gameCode": GAME_CODE,
        "gameKey": GAME_KEY,
        "app_secret": APP_SECRET,
        "apiKey": "1wTOoUwJ4FxhIsoU",
        "api_key": FP_KEY,
        "apiIV": "1234567890abcdef",
        "api_iv": API_IV,
    }

    # ------------------------------------------------------------------
    # 【关键】外层必须包 {code, msg, data}
    # 依据(无争议, 来自游戏自身日志 22:11:09 L6/L8):
    #   FPX Unity receive callback message:
    #     {"module":"platform","func":"init","result":{"code":1,"data":{},"msg":"FPX init success"}}
    #   -> 官方响应外层 = {"code": <int>, "data": <object>, "msg": <string>}
    # 静态反汇编 FPX.dll RVA 0x216EE0 (通用 HTTP 响应处理器) 亦一致:
    #   0x21728B cmp al,7 (顶层 object) -> 0x217D7B 抛 "not an object"
    #   0x2172E2 cmp al,2 ("code" 须 int64) -> 0x217DE4 抛 "not an int64"
    #   0x21731A cmp al,7 ("data" 须 object) -> 0x217E48 抛 "not an object"
    # ------------------------------------------------------------------
    _body = {
        # ---------------- config.hpp 顶层字段 ----------------
        "privacy_baseurl": base,
        "notice_baseurl": base,
        "secret": APP_SECRET,
        "tag": CHANNEL_ID,
        "payHost": base,
        "payProject": GAME_CODE,
        "appId": APP_ID,
        "api_secret": FP_SECRET,
        "api_key": FP_KEY,
        "api_iv": API_IV,
        "channel_1": _channel(1),
        "channel_2": _channel(2),
        "fpPassport": fp_passport,
        "fpAccount": fp_passport,
        "socialauth": {},
        "gamePay": {},
        "funplusIap": {},
        "ZMBiConfig": {},
        "sobot": {},
        "popups": [],
        "sobot_app_key": "",
        "sobot_api_host": "",
        "sobot_channel_id": CHANNEL_ID,
        "sobot_system_num": 0,
        "zcsobot_robot_code": "",
        "zcsobot_robot_alias": "",
        "zcsobot_leave_template_id": "",
        "zcsobot_deploy_id": "",
        "funplusConfig": {
            "fpPassport": fp_passport,
            "privacy_baseurl": base,
            "notice_baseurl": base,
        },
        # ---------------- 登录界面字段(FPXPrivacyJsonObj.hpp / quick_login) ----
        #   csclist + defaultcsc 是同一组
        "csclist": [
            {"csc": "86", "name": "中国大陆", "country": "CN"},
            {"csc": "852", "name": "中国香港", "country": "HK"},
            {"csc": "853", "name": "中国澳门", "country": "MO"},
            {"csc": "886", "name": "中国台湾", "country": "TW"},
        ],
        "defaultcsc": "86",
        "continent": "CN",
        "location": "CN",
        "enable_quick_login": False,
        "default_mobile_csc": "86",
        "default_login_type": "mobile",
        "quick_login_config": {},
        "mobile_csc_country_list_url": base + "/api/csc_list",
        "login_support_type_list": ["mobile", "session_login"],
        "bind_support_type_list": ["mobile"],
        "switch_passport_need_choose_role": False,
        "privacy_agreement": True,
        "user_agreement_link": base + "/agreement/user",
        "privacy_agreement_link": base + "/agreement/privacy",
        "third_agreement_link": base + "/agreement/third",
        "children_agreement_link": base + "/agreement/children",
        "tips_url": base + "/agreement/tips",
        # ---------------- 兼容旧命名(保留, 不影响解析) ----------------
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "channel_id": CHANNEL_ID,
        "sub_channel_id": CHANNEL_ID,
        "package_id": PACKAGE_ID,
        "servers": {
            "account": {"domain": base, "server_name": "FPX账号服务", "configs": {}},
            "auth": {"domain": base, "server_name": "实名认证防沉迷",
                     "configs": {"api_iv": API_IV,
                                 "api_key": AUTH_KEY,
                                 "api_secret": AUTH_SECRET}},
            "fp_account": {"domain": base, "server_name": "FP用户中心",
                           "configs": {"api_iv": API_IV,
                                       "api_key": FP_KEY,
                                       "api_secret": FP_SECRET,
                                       "api_version": "v1",
                                       "sm_app_id": "captcha_cn",
                                       "sm_organization": "xNxMh079HgFDeRkJE1qN"}},
            "notice": {"domain": base, "server_name": "公告-FP", "configs": {}},
            "privacy": {"domain": base, "server_name": "隐私服务", "configs": {}},
            "tracking": {"domain": base, "server_name": "Tracking服务", "configs": {}},
        },
    }
    return {"code": 1, "msg": "Success", "data": _body}


# --------------------------------------------------------------------------
# /api/send_code, /api/send_code_session
# --------------------------------------------------------------------------

def send_code(req):
    """获取验证码。

    前置条件(已改为网页注册制):
      1. 该手机号已在网页端注册
      2. 该手机号存在有效的网页登录会话
    不满足则拒绝, 不会自动建号。
    """
    mobile = str(req.get("mobile") or req.get("phone") or "")
    if not mobile:
        return {"code": -1, "msg": "mobile required"}

    ok, reason = models.can_request_code(mobile)
    if not ok:
        print("[auth] send_code DENIED mobile=%s reason=%s" % (mobile, reason), flush=True)
        return {"code": -1, "msg": reason}

    code = _rand_code(6)
    db.issue_code(mobile, code, ttl=config.CODE_TTL, bound_type=req.get("bind_type"))
    print("[auth] send_code -> mobile=%s code=%s  (web: http://0.0.0.0:8322)" % (mobile, code), flush=True)
    return {"code": 0, "msg": "Success"}


def send_code_session(req):
    return send_code(req)


# --------------------------------------------------------------------------
# /api/login
# --------------------------------------------------------------------------

def login(req):
    mobile = str(req.get("mobile") or req.get("phone") or "")
    code = str(req.get("code") or "")
    if not mobile:
        return {"code": -1, "msg": "mobile required"}

    # 账号必须已注册(网页端), 游戏内不再自动建号
    if not db.get_account_by_mobile(mobile):
        print("[auth] login FAIL mobile=%s reason=not registered" % mobile, flush=True)
        return {"code": -1, "msg": "account not registered (please register on web)"}

    ok, reason = db.verify_code(mobile, code)
    if not ok:
        print("[auth] login FAIL mobile=%s code=%s reason=%s" % (mobile, code, reason), flush=True)
        return {"code": -1, "msg": reason}

    ret, err = models.login(mobile, channel_uid=req.get("channel_uid"))
    if err:
        print("[auth] login FAIL mobile=%s reason=%s" % (mobile, err), flush=True)
        return {"code": -1, "msg": err}

    # ---- 回放开关(文件式, 最可靠) ----
    # 存在 work/data/REPLAY_OFFICIAL 文件时, 原样返回官方真实响应。
    # 用于判定"客户端拒绝"到底是因为响应内容, 还是响应之外的环节。
    import os as _os, json as _json
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _flag = _os.path.join(_root, "work", "data", "REPLAY_OFFICIAL")
    _p = _os.path.join(_root, "work", "data", "official_login_resp.json")
    if _os.path.exists(_flag) and _os.path.exists(_p):
        with open(_p, encoding="utf-8") as _f:
            _off = _json.load(_f)
        print("[auth] login REPLAY-OFFICIAL mobile=%s account_id=%s"
              % (mobile, _off.get("account_id")), flush=True)
        return {"code": 1, "msg": "Success", "data": _off}

    print("[auth] login OK mobile=%s account_id=%s is_new=%s"
          % (mobile, ret["account_id"], ret["is_new"]), flush=True)
    # ------------------------------------------------------------------
    # 【关键】FPX.dll 的 HTTP 响应处理器要求外层包装:
    #   VA 0x217266 -> "code"   0x21728B: cmp al,7 (object) / 0x2172E2: cmp al,2 (int64)
    #   VA 0x2172F5 -> "data"   0x21731A: cmp al,7 (object)
    #   失败分别抛 "not an int64" / "not an object"  (VA 0x195CF60 / 0x195CF20)
    # 扁平返回会导致: at("data") 取不到 -> null -> cmp al,7 失败 -> "not an object"
    # ------------------------------------------------------------------
    return {"code": 1, "msg": "Success", "data": ret}


# --------------------------------------------------------------------------
# /api/session_login
# --------------------------------------------------------------------------

def session_login(req):
    """会话登录(免验证码), 同样要求账号已注册。"""
    mobile = str(req.get("mobile") or req.get("channel_uid") or "")
    if not mobile:
        return {"code": -1, "msg": "mobile required"}
    if not db.get_account_by_mobile(mobile):
        print("[auth] session_login FAIL mobile=%s reason=not registered" % mobile, flush=True)
        return {"code": -1, "msg": "account not registered (please register on web)"}
    ret, err = models.login(mobile, channel_uid=req.get("channel_uid"))
    if err:
        return {"code": -1, "msg": err}
    print("[auth] session_login OK mobile=%s account_id=%s" % (mobile, ret["account_id"]), flush=True)
    return {"code": 1, "msg": "Success", "data": ret}


# --------------------------------------------------------------------------
# 实名 / 防沉迷
# --------------------------------------------------------------------------

def auth_info(req):
    """实名/防沉迷信息。

    字段依据 FPXAuthInfo.hpp + FPXUserJsonObj.hpp:
      FPXAuthInfo.hpp : tip_map / is_adult / auth_status / tip_msg /
                        is_white / is_play / current_tm / conf / remain
      FPXUserJsonObj  : usercenter_data.account_data  (嵌套, 含 is_adult)
    auth_status = 1 表示已实名且成年, 直接放行。
    """
    now = int(time.time())
    # 精确解析路径(FPX.dll 反汇编确证):
    #   1) "auth_status json value result:"      -> 顶层 auth_status (强解析)
    #   2) "usercenter_data.account_data"        -> 进入嵌套
    #   3) "is_adult json value result:"         -> 嵌套里的 is_adult (强解析)
    return {
        "code": 0,
        "msg": "Success",
        # --- 第 1 步: 顶层 auth_status(必须) ---
        "auth_status": 1,
        # --- FPXAuthInfo.hpp 字段表(顺序: tip_map,is_adult,auth_status,
        #     tip_msg,is_white,is_play,current_tm,conf,remain) ---
        "tip_map": {},
        "is_adult": True,
        "tip_msg": "",
        "is_white": True,
        "is_play": True,
        "current_tm": now,
        "conf": {},
        "remain": 0,
        # --- 第 2/3 步: 嵌套结构(必须) ---
        "usercenter_data": {
            "account_data": {
                "is_adult": True,
                "auth_status": 1,
                "is_white": True,
                "is_play": True,
                "remain": 0,
                "current_tm": now,
                "tip_msg": "",
                "tip_map": {},
                "conf": {},
            },
        },
    }


def pre_id_auth(req):
    return {"auth_status": 1, "is_adult": True, "is_play": True, "msg": "Success"}


def id_auth(req):
    return {"auth_status": 1, "is_adult": True, "is_play": True, "msg": "Success"}


# --------------------------------------------------------------------------
# 杂项(全部返回成功, 避免阻塞)
# --------------------------------------------------------------------------

def ping(req):
    """心跳。FPX.dll 用到 played_time / logoff 字段。"""
    return {
        "code": 0,
        "msg": "Success",
        "timestamp": int(time.time()),
        "played_time": 0,
        "logoff": False,
    }


def privacy(req):
    """隐私协议。

    字段依据 FPXPrivacyJsonObj.hpp + FPXHttpRetJsonObj.hpp:
      package_id / pkgChannel / gameConfig / Severity / csclist / defaultcsc
      以及四个协议链接(基类 FPXHttpRetJsonObj.hpp 提供)。
    """
    base = "https://%s" % config.PUBLIC_HOST
    return {
        "code": 0,
        "msg": "Success",
        "package_id": PACKAGE_ID,
        "pkgChannel": PACKAGE_ID,
        "gameConfig": {},
        "Severity": "info",
        "user_agreement_link": base + "/agreement/user",
        "privacy_agreement_link": base + "/agreement/privacy",
        "third_agreement_link": base + "/agreement/third",
        "children_agreement_link": base + "/agreement/children",
        "user": {},
        "privacy": {},
        "third": {},
    }


def announce(req):
    """公告/弹窗。

    字段依据 FPX.dll: show_interval / popups_list / page_size /
                      page_index / timestamp / announce_md5
    """
    return {
        "code": 0,
        "msg": "Success",
        "popups_list": [],
        "show_interval": 0,
        "page_size": 10,
        "page_index": 1,
        "timestamp": int(time.time()),
        "announce_md5": "",
    }


def switch_bind(req):
    """换绑。FPX.dll: FPX_FUNC SWITCH BIND result:"""
    return {"code": 0, "msg": "Success"}


def switch_check(req):
    return {"code": 0, "msg": "Success", "data": {}}


def unregister_cancel(req):
    return {"code": 0, "msg": "Success"}


def get_token(req):
    mobile = str(req.get("mobile") or "")
    acc = db.get_account_by_mobile(mobile) if mobile else None
    return {"code": 0, "msg": "Success", "token": (acc or {}).get("session_key", FP_KEY)}


def social_login(req):
    """第三方登录。"""
    return {"code": 0, "msg": "Success", "is_bind": False}


def social_query(req):
    """查询绑定状态。FPX.dll: is_bind"""
    return {"code": 0, "msg": "Success", "is_bind": False, "social_data": {}}


def social_bind(req):
    """绑定第三方。FPX.dll: FPX_FUNC_SOCIAL_BIND result: is_bind"""
    return {"code": 0, "msg": "Success", "is_bind": True, "social_data": {}}


def social_unbind(req):
    """解绑第三方。"""
    return {"code": 0, "msg": "Success", "is_bind": False, "social_data": {}}


def order(req):
    """订单/支付。字段依据 FPXOrderJsonObj.hpp:
    channel_name / pay_url / rank / icon_url / subscript / sub_channel_id
    """
    return {
        "code": 0, "msg": "Success",
        "channel_name": CHANNEL_ID,
        "pay_url": "",
        "rank": 0,
        "icon_url": "",
        "subscript": "",
        "sub_channel_id": CHANNEL_ID,
        "channels": [],
        "order_id": "",
        "payment_order_id": "",
    }


# 区号列表(登录页 "+86" 下拉用)
CSC_LIST = [
    {"csc": "86", "name": "中国大陆", "country": "CN", "en": "China"},
    {"csc": "852", "name": "中国香港", "country": "HK", "en": "Hong Kong"},
    {"csc": "853", "name": "中国澳门", "country": "MO", "en": "Macao"},
    {"csc": "886", "name": "中国台湾", "country": "TW", "en": "Taiwan"},
    {"csc": "1", "name": "美国", "country": "US", "en": "United States"},
    {"csc": "81", "name": "日本", "country": "JP", "en": "Japan"},
    {"csc": "82", "name": "韩国", "country": "KR", "en": "Korea"},
    {"csc": "65", "name": "新加坡", "country": "SG", "en": "Singapore"},
    {"csc": "60", "name": "马来西亚", "country": "MY", "en": "Malaysia"},
    {"csc": "44", "name": "英国", "country": "GB", "en": "United Kingdom"},
]


def csc_list(req):
    """区号列表。客户端从 get_config 的 mobile_csc_country_list_url 拉取。"""
    return {"code": 0, "msg": "Success", "csclist": CSC_LIST, "list": CSC_LIST}


# --------------------------------------------------------------------------
# 路由表
# --------------------------------------------------------------------------

ROUTES = {
    "/api/get_config":        get_config,
    "/api/send_code":         send_code,
    "/api/send_code_session": send_code_session,
    "/api/login":             login,
    "/api/session_login":     session_login,
    "/api/auth_info":         auth_info,
    "/api/pre_id_auth":       pre_id_auth,
    "/api/id_auth":           id_auth,
    "/api/ping":              ping,
    "/api/privacy/privacy":   privacy,
    "/sdk/announce":          announce,
    "/api/switch_bind":       switch_bind,
    "/api/switch_check":      switch_check,
    "/api/unregister/cancel": unregister_cancel,
    "/self_assist/get_token": get_token,
    "/api/csc_list":          csc_list,
    "/api/csclist":           csc_list,
    "/api/social_login":      social_login,
    "/api/social_query":      social_query,
    "/api/social_bind":       social_bind,
    "/api/social_unbind":     social_unbind,
    "/api/order":             order,
    "/api/cn/channels":       order,
    "/api/cn/place_order":    order,
    "/sdk/event/detail":      announce,
    "/api/social_login":      social_login,
}
