# OpenOrange —— 归龙潮私服复活工程

> **从这里开始：[`docs/项目总览.md`](docs/项目总览.md)** —— 项目全貌、组件状态、协议进度、工具链、踩坑清单。

---

## 一句话现状

**原版客户端 + 启程式注入 + 自研服务器**：登录、选服、网关握手、账号绑定、角色绑定**全部打通**，
目前卡在「角色绑定之后如何进入游戏世界」这一步。

游戏本体**一个字节都没改**。

---

## 目录结构

```
OpenOrange/
├── docs/                  ★ 所有文档（先看 项目总览.md）
│   ├── 项目总览.md        项目入口
│   ├── 协议/              协议逆向成果（权威：完整协议恢复.md）
│   ├── 规划/              早期规划
│   └── 运维/              实机测试指南
│
├── launcher/              ★ Rust 启动器 + 注入 DLL
│   ├── crates/
│   │   ├── oo_launcher/   启动器（Win32 原生 UI + --cli 模式）
│   │   ├── oo_internal/   注入 DLL（挂钩 lua_load + 脱壳 + dump）
│   │   ├── oo_shared/     共享内存协议 + 磁盘兜底
│   │   ├── oo_selfcheck/  自检（7+1 段）
│   │   ├── oo_injector/   独立注入器（含 --probe 探针）
│   │   └── oo_hosttest/   宿主测试器（不启动游戏验证脚本）
│   ├── lua/oo_bootstrap.lua   注入的引导脚本
│   └── dist/              产物 + 脱壳/反编译的 dump
│
├── game_server/           ★ 游戏服（进行中）
│   ├── ms.py              协议实现（protobuf + msgpack + 各消息构造）
│   └── server.py          多模式服务器（--ms-ok / --gate-ok / --proxy / ...）
│
├── web/app.py             后端登录 + 门户        HTTP 8399
├── shared/                SQLite 数据层（账号/验证码/票据/会话）
├── auth_server/           假 fp-passport         HTTPS 8443（备用路线）
├── serverlist_server/     服务器列表            HTTPS 8080（已被 ms 路线取代）
├── tools/                 代理、证书、启动脚本、各类分析脚本
└── work/                  抓包、反编译产物、Ghidra 脚本
```

---

## 快速开始

### 1. 起后端
```powershell
cd F:/haowancheng/OpenOrange
python -u -m web.app                       # 登录/门户  8399
python -u -m game_server.server --ms-ok    # 游戏服     33100(ms) + 33101(网关)
```

### 2. 起启动器
```powershell
cd F:/haowancheng/OpenOrange/launcher/dist
.\OO_Launcher.exe                                   # GUI
.\OO_Launcher.exe --cli 15900159000 oologin123      # CLI（能看进度）
```

**⇒ 游戏会在约 40 秒后加载完 xlua.dll，然后自动完成登录并进入主界面。**

### 3. 看日志
```
launcher/dist/work/logs/launcher.log    启动器日志
launcher/dist/oo_internal.log           注入脚本日志（所有探测输出都在这）
```

---

## 测试账号

| 手机号 | 密码 | account_id |
|---|---|---|
| 15900159000 | oologin123 | oo_08bed76013b06783 |
| 15900159001 | oologin456 | oo_e1019fb0be3debf3 |

---

## 关键约束（改代码前必读）

1. **不要改游戏本体** —— 不改 `GuiLongchao.exe` / `GameAssembly.dll` / 任何 `.lua` / `.bytes`
2. **服务器地址只从 `launcher/dist/OpenOrange.ini` 读**，不要编译进程序
3. **所有结论必须有证据** —— 源码行号 / RVA / 抓包字节 / 日志行

---

## 相关外部路径

| 路径 | 内容 |
|---|---|
| `F:/haowancheng/GuiLongchao/game/` | 游戏本体 |
| `F:/haowancheng/archive/game_data/LuaAll_Decompiled2/` | 11,618 个反编译 Lua |
| `F:/haowancheng/work/qicheng/` | 闭源私服参考件 |
| `F:/haowancheng/work/decompiled/Assembly-CSharp/` | 2,172 个反编译 .cs |
| `F:/ghidra/ghidra_12.1.4_PUBLIC/` | Ghidra |
| `F:/java/temurin_21/` | JDK 21（Ghidra 用） |
