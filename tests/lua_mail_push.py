#!/usr/bin/env python3
"""同源 Lua 推送测试：把服务端产出的 MicroPush 帧，喂给客户端真身的分发器。

用法:
    python tests/lua_mail_push.py

## 它验的是什么

不是"我们字段名对不对"，而是"客户端收到这帧之后，邮件到底进没进列表"。
跑的是真身：

    core/microservice/msservicemanager.lua   MsServiceManager:onPush（方法名拼接 + 端点查找）
    entities/avatarcomponent/avatarmailcomp.lua  MailService_newMailNotice（真实加邮件 + 红点）

只替身 `ui.postEvent` / `EUIEventDefine`（纯 UI 通知，不改列表状态）。

## 被钉住的两件事

    P1  服务端帧 → onPush → 邮件进 client 的 userMailList，hadNewMail=true
    P2  如果订阅者注册成"没有该方法的对象"，消息会被 onPush 丢掉 ——
        这正是注入脚本要把订阅者换成 ClientAvatar 的原因。
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_pushlua_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import send as mailsend  # noqa: E402
from game_server.mail import template  # noqa: E402
from game_server.transport import ms_conns  # noqa: E402
from tests.lua_harness import LUA_ROOT, new_runtime  # noqa: E402

UID = "push_lua_u"
TPL = 1
passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


class FakeSock:
    def __init__(self):
        self.frames = []

    def sendall(self, data):
        self.frames.append(bytes(data))


#: 直接把 MicroPush 的 params 字节解出来喂给 Lua（不从 dict 抄，避免"两边各自编"）
def push_params_from_frame(frame):
    body = msmod.pb_parse(frame[6:])
    return msmod.mp_decode(body[3][0])[0]


_CHUNK = r"""
local params, uid, use_bad_target = ...
require("GameCommon.Helper.LuaExtension")
ui = { postEvent = function() end }
EUIEventDefine = { EMailRedDotChanged = true }

local SM = require("Core.MicroService.MsServiceManager")
local MC = require("Entities.AvatarComponent.AvatarMailComp")

local mgr = SM({})
-- 目标：一个「和 ClientAvatar 一样」的表（有混入的 MailService_newMailNotice）
local target = setmetatable({}, { __index = MC })
target.userMailList = {}
target.mailKeyList = {}
target.logger = logger
target.uid = uid

if use_bad_target then
  -- 模拟修复前的注册目标：一个没有 MailService_newMailNotice 的对象（= MsCommon 组件）
  mgr:addPushEndpoint(uid, { notAMailTarget = true })
else
  mgr:addPushEndpoint(uid, target)
end

mgr:onPush("ENTITY", { uid }, "", "MailService", "newMailNotice", params)

local n = 0
for _ in pairs(target.userMailList) do n = n + 1 end
local first = nil
for _, m in pairs(target.userMailList) do first = m break end
return { count = n, hadNew = target.hadNewMail == true,
         title = first and first.Title or "", mailId = first and first.MailId or 0 }
"""


def boot():
    lua = new_runtime(now=1_700_000_000)
    root = LUA_ROOT.replace(chr(92), "/")
    lua.execute('package.path = "' + root + '/lib/?.lua;" .. package.path')
    lua.execute('package.preload["json"] = function() '
                'return assert(loadfile("' + root + '/lib/json.lua"))() end')
    return lua, lua.globals().load(_CHUNK)


def _plain(v):
    if hasattr(v, "items"):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def main():
    print("tmp =", _TMP)
    if template.template(TPL) is None:
        print("SKIP：没有 BDD MailData")
        return 0
    lua, fn = boot()

    # ---------------------------------------------------------- P1
    print("\n[P1] 服务端帧 -> onPush -> 邮件进客户端列表")
    # ★ 必须走真正的登录快照：props["uid"] 是在登录初始化里写的，
    #   空 dict 起的状态没有 uid -> push notify 会以「状态里没有 uid」跳过。
    path = os.path.join(os.environ["OO_STATE_DIR"], UID + ".json")
    if os.path.exists(path):
        os.remove(path)
    pstate.STATES.pop(UID, None)
    msmod.live_init_dict(uid=UID)
    st = pstate.STATES[UID]
    sock = FakeSock()
    ms_conns.reset_for_tests()
    ms_conns.register(1, sock)
    ms_conns.bind_uid(1, UID)
    mailsend.send_mail(st, TPL)
    check("服务端确实推了一帧", len(sock.frames) == 1, len(sock.frames))
    if not sock.frames:
        return _report()

    params = push_params_from_frame(sock.frames[0])
    check("params 是 [mailsInfo] 形状",
          isinstance(params, list) and len(params) == 1 and isinstance(params[0], list),
          type(params))
    res = _plain(fn(lua.table_from(params, recursive=True), UID, False))
    check("客户端加进了 1 封邮件", res["count"] == 1, res)
    check("是新邮件（红点会亮）", res["hadNew"] is True, res)
    server_mail = msmod.mp_decode(msmod.pb_parse(sock.frames[0][6:])[3][0])[0][0][0]
    check("MailId 一致", int(res["mailId"]) == server_mail["MailId"], (res["mailId"], server_mail["MailId"]))
    check("Title 一致", res["title"] == server_mail["Title"], (res["title"], server_mail["Title"]))

    # ---------------------------------------------------------- P2
    print("\n[P2] 订阅者没有该方法 -> 消息被 onPush 丢掉（所以注入脚本要换目标）")
    res2 = _plain(fn(lua.table_from(params, recursive=True), UID, True))
    check("邮件没进列表", res2["count"] == 0, res2)
    check("也没有置新邮件标记", res2["hadNew"] is False, res2)

    ms_conns.reset_for_tests()
    return _report()


def _report():
    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
