//! OO_Launcher —— 归龙潮私服启动器（原生 Win32 UI）。
//!
//! 职责：
//!   1. 读取同目录 OpenOrange.ini（服务器地址完全外部化）
//!   2. 提供原生账号/密码界面
//!   3. 向我们的后端请求登录，拿到与官服同构的 {code,msg,data}
//!   4. 挂起方式拉起游戏、创建共享内存、注入 OO_Internal.dll、放行
//!
//! 两种模式：
//!   默认            图形界面
//!   --cli <账号> <密码>   命令行，走完全相同的编排步骤（便于自动化与排障）

#![windows_subsystem = "windows"]

mod bridge;
mod config;
mod inject;
mod log;
mod ui;
mod net;
mod win32;


/// 命令行模式下需要能看到 stdout —— 用 AttachConsole 把父控制台接回来。
#[cfg(windows)]
fn attach_parent_console() {
    use windows::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
    unsafe {
        let _ = AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    if let Some(i) = args.iter().position(|a| a == "--cli") {
        attach_parent_console();
        let username = args.get(i + 1).cloned().unwrap_or_default();
        let password = args.get(i + 2).cloned().unwrap_or_default();
        // ★ --auto：才注入「自动进入游戏」的逻辑。
        //   不加 --auto 时启动器仍会登录后端并拉起游戏，但不会接管
        //   platform/login，游戏会停在它自己的登录界面（方便观察原始流程）。
        let auto = args.iter().any(|a| a == "--auto");
        if username.is_empty() || password.is_empty() {
            eprintln!("用法: OO_Launcher.exe --cli <账号> <密码> [--auto]");
            eprintln!("  --auto  自动进入游戏（不加则只拉起客户端，停在登录界面）");
            std::process::exit(2);
        }
        // 日志与配置初始化在 ui::run_cli -> init_shared 内完成
        let code = ui::run_cli(&username, &password, auto);
        std::process::exit(code);
    }

    ui::run();
}