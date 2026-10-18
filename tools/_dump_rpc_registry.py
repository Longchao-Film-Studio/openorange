# -*- coding: utf-8 -*-
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG = r"F:/haowancheng/OpenOrange/launcher/dist/oo_internal.log"
OUT = r"F:/haowancheng/OpenOrange/docs/协议/实体RPC注册表.md"

pat = re.compile(r"registerRpc\[(\d+)\]: (.+?) -> (\d+)")
rows = []
for ln in open(LOG, encoding="utf-8", errors="replace"):
    m = pat.search(ln)
    if m:
        rows.append((int(m.group(1)), m.group(2), int(m.group(3))))

rows.sort()
lines = []
lines.append("# 实体 RPC 注册表（运行时实测）")
lines.append("")
lines.append("> 来源：在注入脚本里包住 `phonestcore.registerRpc`，记录每一次注册的名字与返回值。")
lines.append("> 返回值是 32 位哈希，也就是该 RPC 在**实体消息**层的唯一标识。")
lines.append("> 实测共 **%d** 条。**协议层 RPC（sendConnectServer / connectResponse / onMicroResponse 等）不在此表中** —— "
             % len(rows))
lines.append("> 它们由原生层/C# 层注册，Lua 侧观察不到。")
lines.append("")
lines.append("| # | 方法名 | 哈希 | 十六进制 |")
lines.append("|---|---|---|---|")
for n, name, h in rows:
    lines.append("| %d | `%s` | %d | `0x%08X` |" % (n, name, h, h))
lines.append("")
lines.append("## 几个关键条目")
lines.append("")
for key in ("RpcOnTicketFailed", "RpcOnTicketExpired", "RpcOnNeedUpdateClient",
            "ClientKicked", "RpcNotifyError", "RpcPopToastText"):
    for n, name, h in rows:
        if name == key:
            lines.append("- `%s` = `0x%08X` —— 对应之前定位的「登录认证失败/已失效」弹窗链路"
                         % (name, h) if "Ticket" in key else
                         "- `%s` = `0x%08X`" % (name, h))
            break

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("写入", OUT, "共", len(rows), "条")