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
├── docs/                  ★ 所有文档
│   ├── 逆向过程记录.md    ★ 协议是怎么挖出来的（含 15 条踩坑）
│   ├── 配置说明.md        ★ OpenOrange.ini 教程与示例
│   ├── 项目总览.md        项目全貌与组件状态
│   ├── 协议/              协议逆向成果（权威：完整协议恢复.md）
│   ├── 规划/              早期规划
│   └── 运维/              实机测试指南
│   （部署说明就在本文件下面：「拿到源码之后怎么部署」）
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
│   └── dist/              运行目录（从 target/release 拷产物进来）
│
├── game_server/           ★ 游戏服（进行中）
│   ├── ms.py              协议实现（protobuf + msgpack + 各消息构造）
│   └── server.py          多模式服务器（--ms-ok / --gate-ok / --proxy / ...）
│
├── my_scripts/            可复用的分析脚本（协议逆向用）
├── archivescript/         一次性过程脚本（已归档，保留作证据）
│
├── web/app.py             后端登录 + 门户        HTTP 8399
├── shared/                SQLite 数据层（账号/验证码/票据/会话）
├── auth_server/           假 fp-passport         HTTPS 8443（备用路线）
├── serverlist_server/     服务器列表            HTTPS 8080（已被 ms 路线取代）
├── tools/                 代理、证书、安装脚本
├── work/                  抓包记录、Ghidra 脚本（可再生的产物已 gitignore）
└── start_server.py        一键起后端 + 游戏服
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
---

# 拿到源码之后怎么部署

> **仓库里只有源码。** 游戏本体、编译产物、脱壳镜像、dump 出来的程序集都不在仓库里
> （前两者体积大，后者涉及版权，且都能重新生成）。

## 前置要求

| 项 | 版本 | 必需? | 说明 |
|---|---|---|---|
| **Rust** | nightly | ✅ 必需 | `rust-toolchain.toml` 指定了 nightly |
| **Python** | 3.10+ | ✅ 必需 | 后端 + 游戏服 |
| **原版游戏客户端** | - | ✅ 必需 | 仓库不含，需自行获取 |
| Ghidra | 12.1.4 | 可选 | 只在要重新分析原生代码时用 |
| JDK | 21 | 可选 | Ghidra 依赖 |
| ilspycmd | 最新 | 可选 | 只在要反编译 C# 时用：`dotnet tool install --global ilspycmd` |

---

## 第 1 步：编译（这一步会自动完成）

```powershell
cd <仓库路径>/launcher
cargo build --release
```

**⇒ 会产出两个文件：**
```
target/release/OO_Launcher.exe      约 345 KB
target/release/OO_Internal.dll      约 260 KB
```

**⇒ ✅ 实测：从空目录 clone 下来能直接编译成功。**

## 第 2 步：把产物拷进 dist

**⇒ dist 目录是运行目录，但仓库里只保留了 `OpenOrange.ini`（产物已被 gitignore）。**

```powershell
cd <仓库路径>/launcher
Copy-Item target/release/OO_Launcher.exe dist/
Copy-Item target/release/OO_Internal.dll dist/
```

**⇒ 拷完后 `launcher/dist/` 应该是：**
```
OO_Launcher.exe      启动器
OO_Internal.dll      注入模块
OpenOrange.ini       配置
work/                日志目录（首次运行自动创建）
```

## 第 3 步：准备游戏本体

**⇒ 把原版客户端放到某处，然后让 `OpenOrange.ini` 指向它。**

**⇒ 两种方式，任选：**

**方式 A：改 ini 里的 `game_exe`**
```ini
[Game]
game_exe = D:\Games\GuiLongchao\game\GuiLongchao.exe
```
**⇒ 支持绝对路径，也支持相对**启动器所在目录**的相对路径。**

**方式 B：把启动器文件挪到游戏旁边**
**⇒ 把 `OO_Launcher.exe` / `OO_Internal.dll` / `OpenOrange.ini` 三个文件拷到游戏
⇒ `GuiLongchao.exe` 同级的目录，然后把 ini 里的 `game_exe` 留空。**

**⇒ ⚠️ 注意：`game_exe` 写死了不存在的路径会**直接报错**，不会偷偷去别处找 —— 这是刻意的，
⇒ 为的是让你能判断到底启动了哪个程序。报错信息里会打印实际解析出的路径。**

## 第 4 步：起服务

```powershell
cd <仓库路径>
python start_server.py
```

**⇒ 会开两个独立窗口：**
```
OO-Backend:8399        后端（登录 + 服务器名）
OO-GameServer          游戏服 33100(微服务) + 33101(游戏网关)
```

**⇒ 然后自动检查端口，应该看到三个 `监听中`。**

**⇒ 也可以分开起：**
```powershell
python start_server.py --backend    # 只起后端
python start_server.py --game       # 只起游戏服
```

**⇒ 或者手动起（不想用脚本时）：**
```powershell
python -u -m web.app --port 8399
python -u -m game_server.server --ms-ok
```

## 第 5 步：启动游戏

**⇒ 再开一个窗口：**

```powershell
cd <仓库路径>/launcher/dist
.\OO_Launcher.exe --cli 15900159000 oologin123
```

**⇒ 成功的话会看到：**
```
[1/6] 注入模块 = ...\OO_Internal.dll
[2/6] 游戏客户端 = ...\GuiLongchao.exe
[3/6] 请求登录 = http://127.0.0.1:8399/api/launcher/login
[4/6] 登录成功 account_id=oo_08bed76013b06783 ticket_len=492
[5/6] 游戏已挂起启动 pid=xxxxx
     已注入 ...\OO_Internal.dll
[6/6] 游戏已放行
```

**⇒ ⚠️ 这个窗口不要关 —— 关掉会连带关掉游戏。**

**⇒ 然后等 40~60 秒（游戏要加载 `xlua.dll`，实测需 36~42 秒），游戏会自动完成登录进主界面。**

---

## 部署后的自检

### 看注入日志
```powershell
Get-Content <仓库路径>/launcher/dist/oo_internal.log -Tail 30
```

**⇒ 成功的标志（顺序固定）：**
```
hook installed: body=0x...
inject: verify -> true|function|function
lua: account: sdk login delivered account_id=...
lua: account: EnterGame invoked
lua: serverlist: installed, GetOneServer=2000
lua: servermgr.onDone 已包裹（列表保护）
serverlist: onDone 后强制盖回 —— 名字=OpenOrange
```

### 看服务端有没有收到流量
**⇒ 切到 `OO-GameServer` 窗口，点游戏里的「开始游戏」，应该看到：**
```
[ms] RX ... service=ClientGate method=syncMetadata
[ms] TX MsResponse ...
[ms] RX ... service=GameLoadService method=query
[ms] TX MsResponse ...
[GATE] * 收到网关连接
[GATE] proto.reqType=0  -> TX ConnectServerResp + bindAccount
[GATE] proto.reqType=4  -> TX ConnectServerResp + bindAvatar
[GATE] proto.reqType=3  -> TX ConnectServerResp
```

**⇒ 走到 `reqType=3` 就说明**账号绑定 + 角色绑定都成功了**。**

---

## 常见部署问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `cargo build` 报 toolchain 错 | 没装 nightly | `rustup toolchain install nightly` |
| 启动器报「找不到注入模块」 | 产物没拷进 dist | 执行第 2 步 |
| 启动器报「找不到游戏客户端」 | `game_exe` 错 | 按报错里给的路径核对；或改用方式 B |
| 端口被占用 | 有旧的 python 进程 | `Get-NetTCPConnection -LocalPort 8399,33100,33101 -State Listen` 查 PID，按 PID 精确关掉 |
| 注入日志里有 `xlua.dll not loaded` | 游戏启动慢或没起来 | 等 5 分钟；确认游戏进程真的在跑 |
| 游戏内服务器名还是官方名 | 引导脚本没生效 | 看日志里有没有 `serverlist:` 相关行 |
| 点开始游戏没反应 | 游戏服没起 | 看游戏服窗口是否收到 `syncMetadata` |

---

## 想重新生成逆向产物

**⇒ 仓库不含这些（体积大 / 可再生），需要时自己跑一遍：**

| 产物 | 怎么生成 |
|---|---|
| `xlua.dll_unpacked.bin`（24 MB 脱壳镜像） | 注入 DLL 时自动生成，落在 `dist/` |
| `dump_*_net.bin`（16 个解密程序集） | 同上，注入后自动扫描内存 dump |
| 反编译的 `.cs` 文件 | `ilspycmd dump_024_*.bin -o out -p` |
| 反编译的 Lua（11,618 个） | 需自行用 xLuaDumper 类工具处理游戏资源 |

**⇒ 前两项只要跑一次游戏就会自动生成。**
