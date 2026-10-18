# OpenOrange · 归龙潮私服

用**原版游戏客户端**跑自建服务器的完整实现：一个 Rust 启动器 + 一套 Python 服务端。

> **游戏本体一个字节都不改。** 启动器负责取票据、挂起启动、注入运行时模块；
> 运行时模块在游戏进程内部接住 SDK 登录回调，把客户端导向我们自己的服务器。

---

## 功能状态

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 账号注册 / 登录 | ✅ | 网页注册，启动器登录 |
| 启动器 GUI / CLI | ✅ | Win32 原生界面，无 WebView |
| 运行时注入 | ✅ | 挂起启动 → 注入 → 放行 |
| 服务器列表接管 | ✅ | 游戏内显示自建服务器，不再连官方 |
| 网关握手 / 会话 | ✅ | 连接、会话号、内容类型协商 |
| 账号绑定 / 角色绑定 | ✅ | 两步绑定均通过 |
| 进入游戏世界 | 🚧 | 场景与实体同步开发中 |

---

## 它由什么组成

```
┌──────────────────┐   ① 登录 (HTTP)     ┌────────────────────┐
│  OO_Launcher.exe │ ─────────────────► │  web 后端  :8399   │
│   Win32 原生 UI  │ ◄───────────────── │  账号 / 票据 / 门户 │
└────────┬─────────┘   account_id       └────────────────────┘
         │             + ticket
         │ ② 挂起启动 → 注入 OO_Internal.dll → 放行
         ▼
┌───────────────────────────────────────────────┐
│  GuiLongchao.exe   原版客户端，未做任何改动       │
│                                               │
│   └─ OO_Internal.dll                          │
│        ③ 接住 SDK 登录回调，把启动器给的身份喂进去 │
│        ④ 接管服务器列表                          │
│        ⑤ 把网关地址指向自建游戏服                 │
└────────┬──────────────────────────────────────┘
         │ ⑥ 连接自建网关
         ▼
┌────────────────────┐
│  game_server       │  网关 :33101 + 微服务 :33100
└────────────────────┘
```

---

## 目录结构

```
OpenOrange/
├── launcher/                Rust：启动器 + 运行时注入模块
│   ├── crates/
│   │   ├── oo_launcher/     启动器（GUI + --cli）
│   │   ├── oo_internal/     注入模块（DLL）
│   │   ├── oo_shared/       启动器与 DLL 之间的共享内存协议
│   │   ├── oo_selfcheck/    自检工具
│   │   ├── oo_injector/     独立注入器
│   │   └── oo_hosttest/     宿主测试器（不开游戏验证脚本）
│   ├── lua/oo_bootstrap.lua 注入到游戏里的引导脚本
│   └── dist/                运行目录（编译产物 + OpenOrange.ini）
│
├── web/                     后端：账号、票据、门户页面       :8399
├── game_server/             游戏服：网关 + 微服务            :33100 / :33101
├── shared/                  SQLite 数据层
├── docs/
│   └── 配置说明.md          OpenOrange.ini 完整教程与示例
│
├── start_server.py          一键启动服务端
└── start_server.ps1         同上（PowerShell 包装）
```

---

## 环境要求

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10 / 11 x64 | 启动器是 Win32 原生程序 |
| Rust | nightly | `rust-toolchain.toml` 已固定，rustup 会自动装 |
| Python | 3.10+ | 服务端**只用标准库**，无需 `pip install` |
| 游戏客户端 | 归龙潮 PC 版 | 需自行准备，本仓库不包含 |

---

## 快速开始

### 1. 编译启动器

```powershell
cd launcher
cargo build --release
```

产物在 `launcher/target/release/`：

```
OO_Launcher.exe
OO_Internal.dll
```

### 2. 组装运行目录

把产物拷进 `launcher/dist/`（该目录已有配置模板）：

```powershell
Copy-Item target/release/OO_Launcher.exe dist/
Copy-Item target/release/OO_Internal.dll dist/
```

拷完 `launcher/dist/` 应该是：

```
OO_Launcher.exe
OO_Internal.dll
OpenOrange.ini
```

> `OO_Internal.dll` 必须和 `OO_Launcher.exe` 放在同一目录，启动器按文件名注入。

### 3. 指向游戏客户端

编辑 `launcher/dist/OpenOrange.ini` 里的 `game_exe`：

```ini
[Game]
game_exe = D:\Games\GuiLongchao\game\GuiLongchao.exe
```

两种写法都支持：

- **绝对路径** —— 如上。
- **相对路径** —— 相对启动器所在目录。
- **留空** —— 自动找启动器同目录及上级目录里的 `GuiLongchao.exe`，
  适合把三个文件直接拷到游戏目录旁边。

> `game_exe` 写了不存在的路径会直接报错并打印实际解析结果，不会悄悄去别处找。

### 4. 启动服务端

```powershell
python start_server.py
```

会开两个独立窗口并自动检查端口：

```
OO-Backend:8399      后端（账号 / 票据 / 门户）
OO-GameServer        游戏服  33100(微服务) + 33101(网关)
```

也可以只起一半，或者手动起：

```powershell
python start_server.py --backend       # 只起后端
python start_server.py --game          # 只起游戏服

python -u -m web.app --port 8399       # 手动起后端
python -u -m game_server.server --ms-ok  # 手动起游戏服
```

### 5. 注册账号

浏览器打开 <http://127.0.0.1:8399/>，在「注册 / 登录」里注册一个账号。

手机号当 UID 用，不校验真实性；密码自己定。注册完直接在同一页面登录即可，
**不需要在任何文件里预先写账号**。

### 6. 启动游戏

```powershell
cd launcher/dist
.\OO_Launcher.exe
```

在界面里填上刚才注册的账号密码，点登录。

> **这个启动器窗口不要关** —— 关掉会连带关掉游戏。

游戏要加载运行时模块，实测需要 **36 ~ 42 秒**，之后会自动完成登录并进入主界面，
服务器列表里显示的就是你自己的服务器。

---

## 配置

所有服务器地址都从 `launcher/dist/OpenOrange.ini` 读，**不编译进程序**。
改完 ini 重启启动器即可生效。

最小可用配置：

```ini
[Server]
login_url  = http://127.0.0.1:8399/api/launcher/login
game_host  = 127.0.0.1
game_port  = 33100

[Game]
game_exe   = D:\Games\GuiLongchao\game\GuiLongchao.exe
```

完整配置项、每种场景的写法、以及排错对照表见 **[配置说明.md](docs/配置说明.md)**。

---

## 命令行模式

不想开界面（或要自动化 / 排查问题）时用 `--cli`，步骤与 GUI 完全一致，但能看到进度：

```powershell
.\OO_Launcher.exe --cli <账号> <密码>
```

```
[1/6] 注入模块   = ...\OO_Internal.dll
[2/6] 游戏客户端 = ...\GuiLongchao.exe
[3/6] 请求登录   = http://127.0.0.1:8399/api/launcher/login
[4/6] 登录成功   account_id=... ticket_len=...
[5/6] 游戏已挂起启动 pid=...
      已注入 ...\OO_Internal.dll
[6/6] 游戏已放行
```

---

## 日志与排障

| 文件 | 内容 |
| --- | --- |
| `launcher/dist/work/logs/launcher.log` | 启动器日志 |
| `launcher/dist/oo_internal.log` | 注入模块日志（最详细，看这里） |
| 游戏服窗口 | 服务端收到的每条请求 |

看注入模块日志：

```powershell
Get-Content launcher/dist/oo_internal.log -Tail 30
```

**登录成功的标志**（顺序固定）：

```
hook installed
inject: verify -> true|function|function
lua: account: sdk login delivered
lua: account: EnterGame invoked
lua: serverlist: installed
```

---

## 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `cargo build` 报 toolchain 错 | 没装 nightly | `rustup toolchain install nightly` |
| 启动器提示找不到注入模块 | 产物没拷进 dist | 回到「快速开始」第 2 步 |
| 启动器提示找不到游戏客户端 | `game_exe` 写错 | 按报错里打印的路径核对 |
| 端口被占用 | 有旧的 python 进程 | `Get-NetTCPConnection -LocalPort 8399,33100,33101 -State Listen` 查 PID，按 PID 精确关掉 |
| 等了很久游戏没进主界面 | 客户端加载慢 | 注入日志里等到 `EnterGame invoked` 为止，最长等 5 分钟 |
| 游戏内还是官方服务器名 | 引导脚本没生效 | 检查注入日志里有没有 `serverlist:` 开头的行 |
| 点「开始游戏」没反应 | 游戏服没起 | 看游戏服窗口是否收到 `syncMetadata` |
| 登录报「账号或密码错误」 | 账号没注册 | 到 <http://127.0.0.1:8399/> 注册 |

---

## 开发

```powershell
cd launcher
cargo build --release     # 编译
cargo test                # 单元测试
cargo run -p oo_selfcheck # 自检（配置 / 注入模块 / 共享内存 / 引导脚本）
```

服务端没有任何第三方依赖，改完直接重启进程即可。

---

## 免责声明

本项目仅供**学习、研究与私人保存**使用，与游戏开发商、发行商无任何关系。

- 不包含、也不分发任何游戏本体文件、美术资源或商业素材；
- 使用者需自行合法持有游戏客户端；
- 请勿用于任何商业用途或公开运营；
- 因使用本项目产生的一切后果由使用者自行承担。

如果相关权利方有异议，请联系删除。
