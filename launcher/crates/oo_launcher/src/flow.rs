//! 登录 → 拉起（挂起）→ 共享内存 → 注入 → 放行 的核心流程。
//!
//! 背景（2026-10-24 拆分）：此流程此前在 ui.rs 的 on_login_clicked（GUI）与 run_cli（CLI）
//! 各有一份手工实现（约 200 行 × 2），步骤相同但错误呈现不同——典型的双份维护（R3 违规）：
//! 一边改了注入顺序/兜底逻辑，另一边必然漏。
//!
//! 现收敛为 flow::login_and_inject，用 Reporter trait 把「错误呈现」差异留给调用方：
//!   * GUI（ui.rs）实现 = msgbox + 状态栏
//!   * CLI（ui.rs run_cli）实现 = eprintln
//! 流程顺序（start → resume → finish 不可颠倒）见 inject.rs 头注释。

use crate::bridge::BridgeWriter;
use crate::config::Config;
use crate::inject;
use crate::net::{self, LoginRequest};
use crate::{log_debug, log_error, log_info, log_warn};

/// 流程结果：成功时调用方负责 std::mem::forget(writer/launched)（保持句柄存活）。
pub enum FlowOutcome {
    /// 全部成功。writer/launched 必须由调用方 keep-alive（forget 或托管）。
    Ok(Box<BridgeWriter>, Box<inject::Launched>),
    /// 失败。内部已清理句柄（resume/close），调用方只需呈现消息。
    Failed(String),
}

/// 进度/错误呈现抽象（GUI=msgbox+状态栏；CLI=eprintln）。
pub trait Reporter {
    fn status(&self, msg: &str);
    fn fail(&self, title: &str, msg: &str);
    #[allow(dead_code)]
    fn info(&self, msg: &str);
}

/// 执行完整流程。`auto` = OO_AUTO 环境变量（客户端自动进街区）。
pub fn login_and_inject(
    cfg: &Config,
    username: &str,
    password: &str,
    auto: bool,
    rep: &dyn Reporter,
) -> FlowOutcome {
    // 前置检查（避免白跑网络）
    let dll = cfg.resolve_inject_dll();
    if !dll.is_file() {
        return FlowOutcome::Failed(format!(
            "找不到注入模块：{}\n请确认 OO_Internal.dll 与启动器在同一目录。",
            dll.display()
        ));
    }
    let game = match cfg.resolve_game_exe() {
        Ok(p) => p,
        Err(e) => return FlowOutcome::Failed(format!("找不到游戏客户端：{e}")),
    };

    rep.status("正在向服务器请求登录……");

    let body = serde_json::json!({
        "username": username,
        "password": password,
        "channel_id": cfg.channel.channel_id,
        "platform": cfg.channel.platform,
        "client": "OO_Launcher",
        "version": env!("CARGO_PKG_VERSION"),
    })
    .to_string();

    let req = LoginRequest {
        url: cfg.server.login_url.clone(),
        body,
        timeout_ms: cfg.server.timeout_ms,
    };

    let env = match net::post_login(&req) {
        Ok(e) => e,
        Err(e) => return FlowOutcome::Failed(format!("登录请求失败：\n\n{e}")),
    };
    if !env.is_ok() {
        return FlowOutcome::Failed(format!(
            "服务器拒绝登录：\n\ncode={}\nmsg={}", env.code, env.msg
        ));
    }

    let account_id = env.data.get("account_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let has_ticket = env
        .data
        .get("ticket")
        .and_then(|v| v.as_str())
        .map(|s| !s.is_empty())
        .unwrap_or(false);
    log_info!("login ok: account_id={account_id} ticket_present={has_ticket}");

    if account_id.is_empty() || !has_ticket {
        return FlowOutcome::Failed(format!(
            "服务器响应不完整，缺少 account_id 或 ticket。\n\n原始响应：\n{}",
            env.to_payload()
        ));
    }

    rep.status("登录成功，正在启动游戏……");

    // ★ 把「是否自动进入游戏」传给注入的 DLL（游戏进程继承父进程环境）。
    std::env::set_var("OO_AUTO", if auto { "1" } else { "0" });
    log_info!("auto enter game = {auto}");

    // ---- 拉起游戏（挂起） ----
    let launched = match inject::spawn_suspended(&game, &cfg.game.game_args) {
        Ok(l) => l,
        Err(e) => return FlowOutcome::Failed(format!("无法启动游戏：\n\n{e}")),
    };
    log_info!("game spawned (suspended): pid={} exe={}", launched.pid, game.display());

    // ---- 共享内存并写入登录结果 ----
    let writer = match BridgeWriter::create(launched.pid) {
        Ok(w) => w,
        Err(e) => {
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            return FlowOutcome::Failed(format!("共享内存创建失败：\n\n{e}"));
        }
    };
    let payload = env.to_payload();
    let bridge_name = writer.name.clone();
    match writer.publish(&payload) {
        Ok(()) => log_info!("bridge ready: {} ({} bytes)", bridge_name, payload.len()),
        Err(e) => log_error!("bridge publish failed: {e}"),
    }
    // 磁盘兜底：共享内存随启动器进程退出而销毁，文件不会（CLI 版原有，GUI 版补齐）。
    if let Some(dir) = game.parent() {
        match oo_shared::write_disk_payload(dir, &payload) {
            Ok(p) => log_info!("disk fallback payload: {} ({} bytes)", p.display(), payload.len()),
            Err(e) => log_warn!("disk fallback failed: {e}"),
        }
    }

    // ---- 注入 DLL（start → resume → finish，顺序不可颠倒）----
    let pending = match inject::inject_start(launched.process, &dll) {
        Ok(p) => p,
        Err(e) => {
            log_error!("inject start failed: {e}");
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            return FlowOutcome::Failed(format!("注入失败：\n\n{e}"));
        }
    };

    if let Err(e) = inject::resume(launched.thread) {
        log_error!("resume failed: {e}");
    }

    if let Err(e) = inject::inject_finish(pending) {
        log_error!("inject finish failed: {e}");
        inject::close(&launched);
        return FlowOutcome::Failed(format!("注入失败：\n\n{e}"));
    }
    log_info!("injected: {}", dll.display());
    rep.status("游戏已启动，请在游戏内点击登录按钮");

    if cfg.game.keep_alive {
        let raw = inject::process_raw(&launched);
        std::thread::spawn(move || {
            let h = windows::Win32::Foundation::HANDLE(raw as *mut core::ffi::c_void);
            let code = inject::wait_exit(h);
            log_info!("game exited with code {code}");
        });
    }

    FlowOutcome::Ok(Box::new(writer), Box::new(launched))
}

/// keep-alive 工具：调用方对成功结果执行（保持映射与句柄到进程退出）。
pub fn keep_alive(writer: Box<BridgeWriter>, launched: Box<inject::Launched>) {
    std::mem::forget(writer);
    std::mem::forget(launched);
    log_debug!("login flow finished");
}