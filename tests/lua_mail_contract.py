#!/usr/bin/env python3
"""同源 Lua 契约测试：把服务端产出的邮件喂给**客户端自己的**邮件组件。

用法:
    python tests/lua_mail_contract.py

## 为什么不是 mock

跑的是 `archive/game_data/LuaAll_Decompiled2/entities/avatarcomponent/avatarmailcomp.lua`
的**真身**（连同客户端自己的 `lib/json.lua`），只替身三样东西：

    TimeEx           "现在几点"（固定时间，否则倒计时随真实时间漂移）
    getUserMailList  传输层（真实网络调用；替身后变成同步取下一页 = 客户端的分页递归）
    logger/ui        不改变被测逻辑的基础设施

它验的是"客户端回放我们这串数据之后看到什么"，不是"我们的字段名对不对"。

## 被钉住的四个坑（每个都对应一次真实误解）

    L1  ExpireTime == 0 的邮件在面板里**看不见** —— getUnexpiredMailKeyList 用
        `ExpireTime > now` 过滤，而 checkMailRed 又把 0 当永不过期。
    L2  giftInfo 必须是能 json.decode 的字符串。
    L3  分页公式是 `TotalSize - Start <= 100`，不是"取到空为止"。
    L4  排序三级：未读 > 星标 > CreatedTime 倒序。

## 实现注意（这几条都是踩出来的）

1. `Class.Component` 返回的是**组件类型表**，不能直接调用 —— 实例要
   `setmetatable({}, { __index = ComponentType })`。
2. 一旦 `require` 了这个模块，**之后 `lua.execute("X = 1")` 的全局赋值就不生效了**
   （实测：赋值后 `lua.globals().X` 仍是 None）。原因在客户端 Class.lua 的环境处理，
   不去改它。规避办法：**每段场景写成一个 `load()` 出来的函数，参数进、结果 return 出**，
   完全不依赖全局。
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="oo_maillua_")
os.environ["OO_DB_PATH"] = os.path.join(_TMP, "x.db")
os.environ["OO_UNKNOWN_LOG"] = os.path.join(_TMP, "unknown.tsv")
os.environ["OO_CAPTURE_DIR"] = os.path.join(_TMP, "cap")
os.environ["OO_STATE_DIR"] = os.path.join(_TMP, "state")

from game_server import ms as msmod  # noqa: E402
from game_server import player_state as pstate  # noqa: E402
from game_server.mail import store  # noqa: E402
from game_server.transport import ms_service as svc  # noqa: E402
from tests.lua_harness import LUA_ROOT, new_runtime  # noqa: E402

UID = "mail_lua_u"
NOW = 1_700_000_000

passed = []
failed = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}   {detail}")


# ---------------------------------------------------------------- Lua 场景（自包含）
_PRELUDE = r"""
local mails, NOW = ...
TimeEx = { getServerCorrectionTimeS = function() return NOW end }
require("GameCommon.Helper.LuaExtension")          -- 客户端的 table.contains / table.length
local MC = require("Entities.AvatarComponent.AvatarMailComp")
local c = setmetatable({}, { __index = MC })
c.userMailList = {}
c.mailKeyList = {}
c.starIndex = 0
c.hadNewMail = false
c.logger = logger
c.uid = "x"
for _, m in ipairs(mails) do c:addMail(m) end
c:sortMailList()
"""

_SCENARIO_FEED = _PRELUDE + r"""
local visible = c:getUnexpiredMailKeyList()
local res = { count = 0, visible = #visible, keys = {}, award = {},
              needRefresh = c:needRefreshMailList() }
for _ in pairs(c.userMailList) do res.count = res.count + 1 end
for i, id in ipairs(c.mailKeyList) do res.keys[i] = id end
for id, m in pairs(c.userMailList) do
  local ok, v = pcall(function() return c:isHasMailAward(m) end)
  -- ★ 不能写 ok and v or "ERR" —— v 是 false 时会掉进 or 分支（Lua 经典坑）
  if ok then res.award[tostring(id)] = v
  else res.award[tostring(id)] = "ERR:" .. tostring(v) end
end
return res
"""

_SCENARIO_PAGES = r"""
local pages, NOW = ...
TimeEx = { getServerCorrectionTimeS = function() return NOW end }
require("GameCommon.Helper.LuaExtension")
local MC = require("Entities.AvatarComponent.AvatarMailComp")
local c = setmetatable({}, { __index = MC })
c.userMailList = {}
c.mailKeyList = {}
c.starIndex = 0
c.starIndex = 0
c.hadNewMail = false
c.logger = logger
c.uid = "x"
c.requests = {}
c.pageData = pages
-- 传输层替身，替换的是 getUserMailList（发请求那一段），不是 getUserMailList_cb。
--   真身里 callBack 参数 = CallbackHandler(self,"handleOnePageMailList", CB)；
--   拿到应答后由 getUserMailList_cb 转一手（它判 retStatus.status 再 callBack(response)）。
--   这里就走那个真身函数，别自己拼回调顺序。
function c:getUserMailList(start, count, callBack)
  table.insert(self.requests, { start, count })
  local resp = self.pageData[tostring(start)]
  if resp ~= nil then
    self:getUserMailList_cb(callBack, { status = true, errmsg = "" }, resp)
  end
end
c:getSeverAllMail(nil)
local res = { pages = {}, count = 0, visible = #c:getUnexpiredMailKeyList() }
for i, r in ipairs(c.requests) do res.pages[i] = { r[1], r[2] } end
for _ in pairs(c.userMailList) do res.count = res.count + 1 end
return res
"""


def boot():
    lua = new_runtime(now=NOW)
    root = LUA_ROOT.replace(chr(92), "/")
    # json 必须在 require 邮件组件之前挂好 preload（组件顶层就 require("json")）
    lua.execute('package.path = "' + root + '/lib/?.lua;" .. package.path')
    fn_feed = lua.globals().load(_SCENARIO_FEED)
    fn_pages = lua.globals().load(_SCENARIO_PAGES)
    return lua, fn_feed, fn_pages


def _plain(v):
    if hasattr(v, "items"):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def seed(mails):
    """把邮件灌进玩家状态**一次**，返回状态。

    ★ 以前每取一页就重新灌一遍，于是第 2/3 页的 TotalSize 分别是 500/750 ——
      客户端据此算出还要下一页，分页断言永远对不上。状态只建一次。
    """
    st = pstate.get_state({}, uid=UID)
    for k in list(st.props):
        if k.startswith("_oo_"):
            del st.props[k]
    for m in mails:
        store.add(st.props, m, now=NOW)
    return st


def server_page(start, page_size=100):
    """用**服务端真实处理器**产出一页应答，再按客户端的方式解码。"""
    fields = {4: [msmod.mp_arr([UID, start, page_size])]}
    raw = svc.ms_reply_params("MailService", "getUserMailList", fields, 0)
    return msmod.mp_decode(raw)[0]


def mail_of(i, *, have_read=False, star=False, created=None, gift=None, expire=None):
    params = {"mailTemplateId": 1, "haveRead": have_read, "star": star,
              "giftReceived": False}
    if gift is not None:
        params["giftInfo"] = gift
    return {"MailId": i, "Title": f"邮件{i}", "SrcName": "系统",
            "CreatedTime": NOW - 1000 + i if created is None else created,
            "ExpireTime": (NOW + 86400 * 30) if expire is None else expire,
            "Params": params}


def run_feed(lua, fn, mails):
    return _plain(fn(lua.table_from(mails, recursive=True), NOW))


def main():
    print("tmp =", _TMP)
    lua, fn_feed, fn_pages = boot()

    # ------------------------------------------------------------ L3 分页
    print("\n[L3] 分页：250 封 -> 客户端应该正好请求 3 页（0 / 100 / 200）")
    mails = [mail_of(i) for i in range(250)]
    seed(mails)
    pages = lua.table()
    for start in (0, 100, 200):
        pages[str(start)] = lua.table_from(server_page(start), recursive=True)
    res = _plain(fn_pages(pages, NOW))
    # Lua 数组 -> _plain 转成 {"1": {...}, "2": ...}，按整数序取回，别用 dict 的遍历顺序
    entries = [res["pages"][str(i)] for i in range(1, len(res["pages"]) + 1)]
    starts = [int(e["1"]) for e in entries]
    counts = [int(e["2"]) for e in entries]
    check("正好 3 页", len(starts) == 3, starts)
    check("页码 0/100/200", sorted(starts) == [0, 100, 200], starts)
    check("每页要 100 封", counts == [100, 100, 100], counts)
    check("客户端收满 250 封", res["count"] == 250, res["count"])
    check("全部可见（都没过期）", res["visible"] == 250, res["visible"])

    # ------------------------------------------------------------ L1 ExpireTime=0
    print("\n[L1] ExpireTime == 0 的邮件：客户端列表里看不见（所以服务端必须给非零）")
    r = run_feed(lua, fn_feed, [mail_of(1, expire=0)])
    check("这封邮件确实进了 userMailList", r["count"] == 1, r["count"])
    check("但它不在 getUnexpiredMailKeyList 里", r["visible"] == 0, r["visible"])

    # ------------------------------------------------------------ L2 giftInfo
    print("\n[L2] giftInfo：必须是能 json.decode 的字符串")
    # ★ 这一条用**服务端真身**产出的邮件（template.build），不是手写 dict ——
    #   否则"服务端把 giftInfo 存成对象"这种错误，Lua 侧永远看不出来。
    from game_server.mail import template as _tpl
    built = _tpl.build(1, created=NOW) if _tpl.template(1) else None
    if built is None:
        server_mail = mail_of(1, gift='{"103":1000,"102":30}')
    else:
        server_mail = dict(built)
        server_mail["MailId"] = 1
    r = run_feed(lua, fn_feed, [server_mail, mail_of(2)])
    award = r["award"]
    check("带附件的邮件 isHasMailAward = true", award.get("1") is True, award)
    check("不带附件的邮件 isHasMailAward = false", award.get("2") is False, award)
    # 这两封信是服务端真身造的 —— 顺便钉住"服务端给的 ExpireTime 必须让客户端看得见"
    check("服务端造的邮件在客户端列表里可见（ExpireTime > 0）", r["visible"] == 2, r["visible"])

    r = run_feed(lua, fn_feed, [mail_of(3, gift={"103": 1000})])
    got3 = r["award"].get("3")
    check("giftInfo 传对象会报错（所以服务端必须发字符串）",
          isinstance(got3, str) and got3.startswith("ERR:"), got3)

    # ------------------------------------------------------------ L4 排序
    print("\n[L4] 排序：未读 > 星标 > CreatedTime 倒序")
    r = run_feed(lua, fn_feed, [
        mail_of(11, have_read=True, star=True, created=NOW - 50),
        mail_of(12, have_read=True, star=False, created=NOW - 10),
        mail_of(13, have_read=False, star=False, created=NOW - 90),
    ])
    order = [int(r["keys"][str(i)]) for i in range(1, len(r["keys"]) + 1)]
    check("未读排最前", order[0] == 13, order)
    check("两个已读里星标在前", order[1] == 11, order)

    # ------------------------------------------------------------ 面板决策
    print("\n[L5] needRefreshMailList：空箱必须刷新（否则面板永远不拉数据）")
    r = run_feed(lua, fn_feed, [])
    check("空邮箱 -> 需要刷新", r["needRefresh"] is True, r["needRefresh"])
    r = run_feed(lua, fn_feed, [mail_of(21)])
    check("有未过期邮件 -> 不需要刷新", r["needRefresh"] is False, r["needRefresh"])

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  - " + name)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
