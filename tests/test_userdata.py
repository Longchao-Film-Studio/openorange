#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UserDataService.batchGetAttribute 应答构造测试（阶段 1-3 主数据通道）。

依据：
  entities/avatarcomponent/avataruserdatacomp.lua:117-162  BatchGetUserData 发请求
  entities/avatarcomponent/avataruserdatacomp.lua:164-212  回调怎么用应答

请求：parameters = [ list ]，list = [ [uidStr, [attr, ...]], ... ]
应答：{ Ret = [status, errmsg], Results = [ {Uid, AttributesMap} ] }
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_server import ms as msmod

passed, failed = [], []


def check(name, cond, detail=None):
    if cond:
        passed.append(name)
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  <- " + str(detail) if detail else ""))


def run():
    print("\n[1] 基本结构")
    req = [[["OO00000001", ["player_name", "level", "gender", "signature"]]]]
    raw = msmod.build_userdata_batch_params(req)
    d = msmod.mp_decode(raw)[0]
    check("Ret 是 [True, 0]", d.get("Ret") == [True, 0], d.get("Ret"))
    check("有 Results", isinstance(d.get("Results"), list) and len(d["Results"]) == 1, d.get("Results"))

    info = d["Results"][0]
    check("Uid 是字符串（客户端严格比较）", isinstance(info.get("Uid"), str), type(info.get("Uid")))
    check("Uid 值正确", info.get("Uid") == "OO00000001", info.get("Uid"))
    amap = info.get("AttributesMap") or {}
    # ★ P0-1：新号 player_name 是空串（官方默认），不是显示占位符。
    check("player_name 是空串（官方默认 config.lua:9073-9078）",
          amap.get("player_name") == "", repr(amap.get("player_name")))
    # level 是客户端声明默认值 1；脚手架要拉高请用 --unlock-all（运行时覆盖）
    check("level = 1", amap.get("level") == 1, amap.get("level"))
    # ★ P0-4（2026-10-22）：原来断言 gender == 1，那是在断言 bug。
    #   官方默认 gender = 0（generated/config.lua:9001-9006），0=未选/1=男/2=女。
    #   取名面板靠 gender==0 才判定「未取名」并弹出（viewuinode.lua:1402-1416）。
    check("gender = 0（官方默认，未选）", amap.get("gender") == 0,
          amap.get("gender"))
    check("signature 存在", "signature" in amap, list(amap.keys()))

    print("\n[2] 自定义类型必须跳过")
    # skins / char_list 在 initDict 里是 {__tp__, __id__} 占位，不是客户端要的真实结构，
    # 硬塞会喂错数据；客户端对缺失属性是容忍的。
    req2 = [[["OO00000001", ["skins", "char_list", "level"]]]]
    d2 = msmod.mp_decode(msmod.build_userdata_batch_params(req2))[0]
    amap2 = d2["Results"][0]["AttributesMap"]
    check("skins 被跳过", "skins" not in amap2, list(amap2.keys()))
    check("char_list 被跳过", "char_list" not in amap2, list(amap2.keys()))
    check("level 仍然保留", amap2.get("level") == 1, amap2.get("level"))

    print("\n[3] 未知属性名不报错")
    req3 = [[["OO00000001", ["__not_a_prop__", "level"]]]]
    d3 = msmod.mp_decode(msmod.build_userdata_batch_params(req3))[0]
    amap3 = d3["Results"][0]["AttributesMap"]
    check("未知属性被忽略", "__not_a_prop__" not in amap3, list(amap3.keys()))
    check("level 还在", amap3.get("level") == 1, amap3.get("level"))

    print("\n[4] 多 uid")
    req4 = [[["A1", ["level"]], ["A2", ["gender"]]]]
    d4 = msmod.mp_decode(msmod.build_userdata_batch_params(req4))[0]
    check("Results 2 条", len(d4["Results"]) == 2, len(d4["Results"]))
    check("Uid A1", d4["Results"][0]["Uid"] == "A1", d4["Results"][0]["Uid"])
    check("Uid A2", d4["Results"][1]["Uid"] == "A2", d4["Results"][1]["Uid"])

    print("\n[5] 空请求兜底")
    d5 = msmod.mp_decode(msmod.build_userdata_batch_params([]))[0]
    check("空请求也有 1 条 Results", len(d5.get("Results") or []) == 1, d5.get("Results"))
    check("兜底 Uid 是默认值", d5["Results"][0]["Uid"] == "OO00000001", d5["Results"][0]["Uid"])

    print("\n[6] msgpack 能往返")
    check("解码后是 dict", isinstance(d, dict))
    check("AttributesMap 是 dict", isinstance(amap, dict))

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
