# OpenOrange

> 某死掉的国风横版动作二次元游戏的服务器实现，未完全做完

## 需要什么

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10 / 11 x64 | 启动器是 Win32 原生程序 |
| Rust | nightly | `launcher/rust-toolchain.toml` 已固定，rustup 会自动装 |
| Python | 3.10+ | 服务端只用标准库，不需要 `pip install` |
| 游戏客户端 | 自备 PC 版 | 本仓库不包含，需自行准备 |

## 编译启动器

```powershell
cd launcher
cargo build --release
```

产物在 `launcher/target/release/`：

```
OO_Launcher.exe
OO_Internal.dll
```

两个文件都拷进 `launcher/dist/`，必须放在同一目录（启动器按文件名注入）。

需配置启动器Config，可询问AI。

## 启动服务端

```powershell
.\start_all.ps1          # 起全部（game + web）
.\start_all.ps1 -Only game   # 只起游戏服
.\stop_all.ps1           # 停掉全部
```

用到的端口：

```
8399    后端（账号 / 登录 / 门户）
33100   游戏服 · 微服务
33101   游戏服 · 网关
33102   管理接口（仅 127.0.0.1）
```

## 注册账号

浏览器打开 <http://127.0.0.1:8399/>，切到「注册」页签，填账号名和密码。

## 开始游戏

**1. 指向游戏客户端**

编辑 `launcher/dist/OpenOrange.ini`，把 `game_exe` 改成自己的游戏可执行文件：

```ini
[Game]
game_exe = D:\Games\<游戏目录>\game\<游戏可执行文件>.exe
```

**2. 启动启动器**

```powershell
cd launcher/dist
.\OO_Launcher.exe
```

填上刚注册的账号密码，点登录。

## 免责声明

本项目仅供**学习、研究与私人保存**使用，与游戏开发商、发行商无任何关系。

- 不包含、也不分发任何游戏本体文件、美术资源或商业素材；
- 使用者需自行合法持有游戏客户端；
- 请勿用于任何商业用途或公开运营；
- 因使用本项目产生的一切后果由使用者自行承担。

如果相关权利方有异议，请联系删除。
