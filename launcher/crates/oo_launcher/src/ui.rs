//! 主窗口、消息循环与业务编排。

use crate::bridge::BridgeWriter;
use crate::config::Config;
use crate::inject;
use crate::log::{self, Level};
use crate::net::{self, LoginRequest};
use crate::win32::{self, Controls};
use crate::{log_debug, log_error, log_info, log_warn};
use windows::core::PCWSTR;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::Graphics::Gdi::HBRUSH;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, LoadCursorW, MessageBoxW,
    PostMessageW, PostQuitMessage, RegisterClassW, SetWindowTextW, ShowWindow, TranslateMessage,
    CW_USEDEFAULT, IDC_ARROW, MB_ICONERROR, MB_ICONINFORMATION, MB_OK, MSG, SW_SHOW,
    WINDOW_EX_STYLE, WM_COMMAND, WM_CREATE, WM_DESTROY, WNDCLASSW, WS_CAPTION, WS_MINIMIZEBOX,
    WS_OVERLAPPED, WS_SYSMENU,
};

const ID_USER: i32 = 1001;
const ID_PASSWORD: i32 = 1002;
const ID_LOGIN: i32 = 1003;
const ID_STATUS: i32 = 1004;
const ID_SERVERINFO: i32 = 1005;
const ID_REMEMBER: i32 = 1006;

// windows crate 未导出：禁用/启用窗口
const WM_SETREDRAW: u32 = 0x000B;

/// 自定义消息：后台线程取回服务器显示名后，回主线程更新 UI。
/// WM_APP = 0x8000，取 +1 避免与将来的其它自定义消息冲突。
const WM_APP_SERVER_NAME: u32 = 0x8000 + 1;

struct AppState {
    cfg: Config,
    ini_found: bool,
    controls: Controls,
    h_user: HWND,
    h_password: HWND,
    h_login: HWND,
    h_status: HWND,
    h_serverinfo: HWND,
    h_remember: HWND,
    busy: bool,
}

static mut STATE: Option<AppState> = None;

fn state() -> &'static mut AppState {
    unsafe {
        let p = &raw mut STATE;
        (*p).as_mut().expect("state not initialized")
    }
}

extern "system" fn enable_window(hwnd: HWND, enable: bool) {
    // 用 USER32 的 EnableWindow；windows crate 未在 WindowsAndMessaging 导出，走动态调用。
    #[link(name = "user32")]
    extern "system" {
        fn EnableWindow(hwnd: *mut core::ffi::c_void, benable: i32) -> i32;
    }
    unsafe {
        let _ = EnableWindow(hwnd.0, if enable { 1 } else { 0 });
    }
}

/// 载入配置、初始化日志与全局 STATE。
///
/// ★ 必须被 GUI 与 CLI 两种模式共用。此前 CLI 直接进 run_cli()，
///   STATE 仍是 None，state() 的 expect("state not initialized") 立即 panic；
///   而 #![windows_subsystem = "windows"] 使该 panic 无处输出，进程静默退出（码 0）。
fn init_shared(to_console: bool) {
    let ini = Config::exe_dir().join("OpenOrange.ini");
    let (cfg, ini_found) = Config::load(&ini);

    let level = Level::parse(&cfg.debug.log_level);
    let log_path = if cfg.debug.log_file {
        Some(Config::exe_dir().join("work").join("logs").join("launcher.log"))
    } else {
        None
    };
    log::init(level, to_console || cfg.debug.console || !cfg.debug.log_file, log_path);
    log_info!("=== OO_Launcher 启动 ===");
    log_info!("ini: {} (found={ini_found})", ini.display());
    log_info!("login_url = {}", cfg.server.login_url);
    log_info!("game_host = {}:{}", cfg.server.game_host, cfg.server.game_port);

    unsafe {
        let p = &raw mut STATE;
        *p = Some(AppState {
            cfg,
            ini_found,
            controls: Controls::new(),
            h_user: HWND::default(),
            h_password: HWND::default(),
            h_login: HWND::default(),
            h_status: HWND::default(),
            h_serverinfo: HWND::default(),
            h_remember: HWND::default(),
            busy: false,
        });
    }
}

pub fn run() {
    init_shared(false);

    // ---- 注册窗口类 ----
    let hinstance = unsafe { GetModuleHandleW(PCWSTR::null()).expect("GetModuleHandleW failed") };
    let class_name = win32::wide("OO_LauncherWindowClass");
    let cursor = unsafe { LoadCursorW(None, IDC_ARROW).unwrap_or_default() };
    let wc = WNDCLASSW {
        lpfnWndProc: Some(wndproc),
        hInstance: hinstance.into(),
        lpszClassName: PCWSTR(class_name.as_ptr()),
        hCursor: cursor,
        // COLOR_BTNFACE + 1
        hbrBackground: HBRUSH((16 + 1) as isize as *mut core::ffi::c_void),
        ..Default::default()
    };
    let _ = unsafe { RegisterClassW(&wc) };

    // ---- 创建主窗口 ----
    let title = win32::wide(&state().cfg.display_name_now());
    let hwnd = unsafe {
        CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(class_name.as_ptr()),
            PCWSTR(title.as_ptr()),
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            466,
            300,
            None,
            None,
            Some(hinstance.into()),
            None,
        )
        .expect("CreateWindowExW failed")
    };

    unsafe {
        let _ = ShowWindow(hwnd, SW_SHOW);
    }

    // ---- 消息循环 ----
    let mut msg = MSG::default();
    while unsafe { GetMessageW(&mut msg, None, 0, 0) }.as_bool() {
        unsafe {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

extern "system" fn wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_CREATE => {
            on_create(hwnd);
            LRESULT(0)
        }
        WM_COMMAND => {
            let id = (wparam.0 & 0xFFFF) as i32;
            let code = ((wparam.0 >> 16) & 0xFFFF) as u32;
            if id == ID_LOGIN && code == 0 {
                on_login_clicked(hwnd);
            }
            LRESULT(0)
        }
        WM_APP_SERVER_NAME => {
            // wparam 携带 Box<String> 的裸指针所有权
            let raw = wparam.0 as *mut String;
            if !raw.is_null() {
                let name = unsafe { *Box::from_raw(raw) };
                apply_server_name(hwnd, name);
            }
            LRESULT(0)
        }
        WM_DESTROY => {
            unsafe { PostQuitMessage(0) };
            LRESULT(0)
        }
        _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
    }
}

fn on_create(hwnd: HWND) {
    let st = state();
    let c = &st.controls;

    c.label(hwnd, "账号", 0, 20, 14, 200, 18).unwrap();
    st.h_user = c.edit(hwnd, "", ID_USER, 20, 34, 406, 24, false).unwrap();

    c.label(hwnd, "密码", 0, 20, 66, 200, 18).unwrap();
    st.h_password = c.edit(hwnd, "", ID_PASSWORD, 20, 86, 406, 24, true).unwrap();

    st.h_remember = c.checkbox(hwnd, "记住账号", ID_REMEMBER, 20, 118, 120, 22).unwrap();

    st.h_login = c.button(hwnd, "登  录  并  启  动  游  戏", ID_LOGIN, 20, 148, 406, 34).unwrap();

    st.h_status = c.label(hwnd, "就绪", ID_STATUS, 20, 190, 406, 18).unwrap();

    refresh_serverinfo(hwnd);

    // ---- 后台拉取服务器显示名 ----
    // 需求：窗口标题的服务器名由服务器决定，不再硬编码。
    // 拉取失败不影响使用，保持 ini / 内置默认名。
    spawn_server_name_probe(hwnd);

    if !st.ini_found {
        set_status("警告：未找到 OpenOrange.ini，正在使用内置默认值");
        log_warn!("OpenOrange.ini not found at {}", st.cfg.ini_path.display());
    }

    let _ = WM_SETREDRAW;
}

/// 重新渲染服务器信息栏。
fn refresh_serverinfo(hwnd: HWND) {
    let st = state();
    let info = format!(
        "服务器 {}   登录 {}   游戏服 {}:{}",
        st.cfg.display_name_now(),
        st.cfg.server.login_url,
        st.cfg.server.game_host,
        st.cfg.server.game_port
    );
    if !st.h_serverinfo.is_invalid() {
        win32::set_text(st.h_serverinfo, &info);
    } else {
        let c = &st.controls;
        if let Ok(h) = c.label(hwnd, &info, ID_SERVERINFO, 20, 212, 406, 36) {
            st.h_serverinfo = h;
        }
    }
}

/// 后台线程向 info_url 查询服务器显示名，成功后更新窗口标题与信息栏。
///
/// 规则（需求）：
///   1. ini 里显式写了 server_name  -> 以 ini 为准，不查询
///   2. ini 里留空                  -> 向 info_url 查询
///   3. 查询失败                    -> 退回内置默认名 "OpenOrange"
fn spawn_server_name_probe(hwnd: HWND) {
    let st = state();
    if st.cfg.configured_display_name().is_some() {
        log_info!("display name pinned by ini: {}", st.cfg.display_name_now());
        return;
    }
    let url = st.cfg.server.info_url.trim().to_string();
    if url.is_empty() {
        log_info!("info_url empty; display name = {}", st.cfg.display_name_now());
        return;
    }
    let timeout = st.cfg.server.timeout_ms.min(3000);
    let hwnd_val = hwnd.0 as isize;

    std::thread::spawn(move || {
        match net::fetch_server_name(&url, timeout) {
            Ok(name) => {
                log_info!("server name from {url}: {name}");
                // 回主线程更新 UI（Win32 控件只能在创建它的线程上操作）
                let name_box: Box<String> = Box::new(name);
                let raw = Box::into_raw(name_box);
                unsafe {
                    let _ = PostMessageW(
                        Some(HWND(hwnd_val as *mut core::ffi::c_void)),
                        WM_APP_SERVER_NAME,
                        WPARAM(raw as usize),
                        LPARAM(0),
                    );
                }
            }
            Err(e) => {
                log_warn!("fetch server name failed ({url}): {e}; keeping {}", state().cfg.display_name_now());
            }
        }
    });
}

/// 应用从服务器取回的显示名（在 UI 线程执行）。
fn apply_server_name(hwnd: HWND, name: String) {
    let st = state();
    st.cfg.server.server_name = name.clone();
    let w = win32::wide(&name);
    unsafe {
        let _ = SetWindowTextW(hwnd, PCWSTR(w.as_ptr()));
    }
    refresh_serverinfo(hwnd);
    set_status(&format!("已连接服务器：{name}"));
}

fn set_status(s: &str) {
    let st = state();
    if !st.h_status.is_invalid() {
        win32::set_text(st.h_status, s);
    }
    log_info!("{s}");
}

fn set_busy(busy: bool) {
    let st = state();
    st.busy = busy;
    if !st.h_login.is_invalid() {
        enable_window(st.h_login, !busy);
    }
}

fn msgbox(
    hwnd: HWND,
    text: &str,
    title: &str,
    flags: windows::Win32::UI::WindowsAndMessaging::MESSAGEBOX_STYLE,
) {
    let t = win32::wide(text);
    let c = win32::wide(title);
    unsafe {
        let _ = MessageBoxW(Some(hwnd), PCWSTR(t.as_ptr()), PCWSTR(c.as_ptr()), flags);
    }
}

fn on_login_clicked(hwnd: HWND) {
    let st = state();
    if st.busy {
        return;
    }

    let username = win32::get_text(st.h_user).trim().to_string();
    let password = win32::get_text(st.h_password);

    if username.is_empty() || password.is_empty() {
        msgbox(hwnd, "请填写账号与密码", "提示", MB_OK | MB_ICONINFORMATION);
        return;
    }

    // 本地前置检查，避免白跑一趟网络
    let dll = st.cfg.resolve_inject_dll();
    if !dll.is_file() {
        let m = format!("找不到注入模块：\n{}\n\n请确认 OO_Internal.dll 与启动器在同一目录。", dll.display());
        msgbox(hwnd, &m, "启动失败", MB_OK | MB_ICONERROR);
        set_status("缺失 OO_Internal.dll");
        return;
    }
    // 游戏路径：ini 配了就用配置的（不存在即报错），没配就只认启动器同级目录。
    let game = match st.cfg.resolve_game_exe() {
        Ok(p) => p,
        Err(e) => {
            log_error!("resolve game exe failed: {e}");
            msgbox(hwnd, &e, "启动失败", MB_OK | MB_ICONERROR);
            set_status("找不到游戏客户端");
            return;
        }
    };

    set_busy(true);
    set_status("正在向服务器请求登录……");

    let body = serde_json::json!({
        "username": username,
        "password": password,
        "channel_id": st.cfg.channel.channel_id,
        "platform": st.cfg.channel.platform,
        "client": "OO_Launcher",
        "version": env!("CARGO_PKG_VERSION"),
    })
    .to_string();

    let req = LoginRequest {
        url: st.cfg.server.login_url.clone(),
        body,
        timeout_ms: st.cfg.server.timeout_ms,
    };

    let env = match net::post_login(&req) {
        Ok(e) => e,
        Err(e) => {
            set_busy(false);
            set_status("登录失败");
            log_error!("login request failed: {e}");
            msgbox(hwnd, &format!("登录请求失败：\n\n{e}"), "登录失败", MB_OK | MB_ICONERROR);
            return;
        }
    };

    if !env.is_ok() {
        set_busy(false);
        set_status("登录被拒绝");
        let m = format!("服务器拒绝登录：\n\ncode={}\nmsg={}", env.code, env.msg);
        msgbox(hwnd, &m, "登录失败", MB_OK | MB_ICONERROR);
        return;
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
        set_busy(false);
        set_status("服务器响应缺少 account_id/ticket");
        let m = format!("服务器响应不完整，缺少 account_id 或 ticket。\n\n原始响应：\n{}", env.to_payload());
        msgbox(hwnd, &m, "登录失败", MB_OK | MB_ICONERROR);
        return;
    }

    set_status("登录成功，正在启动游戏……");

    // ---- 拉起游戏（挂起） ----
    let launched = match inject::spawn_suspended(&game, &st.cfg.game.game_args) {
        Ok(l) => l,
        Err(e) => {
            set_busy(false);
            set_status("无法启动游戏");
            log_error!("spawn failed: {e}");
            msgbox(hwnd, &format!("无法启动游戏：\n\n{e}"), "启动失败", MB_OK | MB_ICONERROR);
            return;
        }
    };
    log_info!("game spawned (suspended): pid={} exe={}", launched.pid, game.display());

    // ---- 建共享内存并写入登录结果 ----
    let writer = match BridgeWriter::create(launched.pid) {
        Ok(w) => w,
        Err(e) => {
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            set_busy(false);
            set_status("共享内存创建失败");
            msgbox(hwnd, &format!("共享内存创建失败：\n\n{e}"), "启动失败", MB_OK | MB_ICONERROR);
            return;
        }
    };
    let payload = env.to_payload();
    let bridge_name = writer.name.clone();
    match writer.publish(&payload) {
        Ok(()) => log_info!("bridge ready: {} ({} bytes)", bridge_name, payload.len()),
        Err(e) => log_error!("bridge publish failed: {e}"),
    }

    // ---- 注入 DLL ----
    // 顺序必须是 start -> resume -> finish：
    // 目标进程主线程处于挂起状态时，远程 LoadLibraryW 线程无法跑完，
    // 若先等待再放行会必然超时（这就是此前"注入失败但进程照跑"的原因）。
    let pending = match inject::inject_start(launched.process, &dll) {
        Ok(p) => p,
        Err(e) => {
            log_error!("inject start failed: {e}");
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            set_busy(false);
            set_status("注入失败");
            msgbox(hwnd, &format!("注入失败：\n\n{e}"), "启动失败", MB_OK | MB_ICONERROR);
            return;
        }
    };

    // ---- 放行 ----
    if let Err(e) = inject::resume(launched.thread) {
        log_error!("resume failed: {e}");
    }

    // ---- 确认 LoadLibraryW 真的成功 ----
    if let Err(e) = inject::inject_finish(pending) {
        log_error!("inject finish failed: {e}");
        set_busy(false);
        set_status("注入失败");
        msgbox(hwnd, &format!("注入失败：\n\n{e}"), "启动失败", MB_OK | MB_ICONERROR);
        return;
    }
    log_info!("injected: {}", dll.display());

    set_status("游戏已启动，请在游戏内点击登录按钮");
    set_busy(false);

    if st.cfg.game.keep_alive {
        let raw = inject::process_raw(&launched);
        std::thread::spawn(move || {
            let h = windows::Win32::Foundation::HANDLE(raw as *mut core::ffi::c_void);
            let code = inject::wait_exit(h);
            log_info!("game exited with code {code}");
        });
    }

    // 保持映射与进程句柄存活到启动器退出
    std::mem::forget(writer);
    std::mem::forget(launched);
    log_debug!("login flow finished");
}
// ============================================================================
// 命令行编排：与 on_login_clicked 完全相同的步骤，只是输入来自 argv。
// 用途：在无法操作 GUI 的环境下跑通完整流程（登录 -> 拉起 -> 注入 -> 放行）。
// 每个失败点都会明确打印，绝不静默。
// ============================================================================
pub fn run_cli(username: &str, password: &str, auto: bool) -> i32 {
    // ★ 通过环境变量把「是否自动进入游戏」传给注入的 DLL（游戏进程继承父进程环境）。
    //   DLL 会在加载引导脚本前把它写成 Lua 全局 OO_AUTO。
    std::env::set_var("OO_AUTO", if auto { "1" } else { "0" });
    init_shared(true);
    let st = state();
    log_info!("=== 命令行模式：{username}（auto={auto}）===");

    let dll = st.cfg.resolve_inject_dll();
    if !dll.is_file() {
        eprintln!("[FAIL] 找不到注入模块: {}", dll.display());
        return 1;
    }
    println!("[1/6] 注入模块 = {}", dll.display());

    let game = match st.cfg.resolve_game_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[FAIL] {e}");
            return 1;
        }
    };
    println!("[2/6] 游戏客户端 = {}", game.display());

    // ---- 登录 ----
    let body = serde_json::json!({
        "username": username,
        "password": password,
        "channel_id": st.cfg.channel.channel_id,
        "platform": st.cfg.channel.platform,
        "client": "OO_Launcher",
        "version": env!("CARGO_PKG_VERSION"),
    })
    .to_string();

    let req = LoginRequest {
        url: st.cfg.server.login_url.clone(),
        body,
        timeout_ms: st.cfg.server.timeout_ms,
    };
    println!("[3/6] 请求登录 = {}", req.url);

    let env = match net::post_login(&req) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("[FAIL] 登录请求失败: {e}");
            return 1;
        }
    };
    if !env.is_ok() {
        eprintln!("[FAIL] 服务器拒绝: code={} msg={}", env.code, env.msg);
        return 1;
    }
    let account_id = env
        .data
        .get("account_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let ticket = env
        .data
        .get("ticket")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if account_id.is_empty() || ticket.is_empty() {
        eprintln!("[FAIL] 响应缺少 account_id/ticket: {}", env.to_payload());
        return 1;
    }
    println!("[4/6] 登录成功 account_id={account_id} ticket_len={}", ticket.len());

    // ---- 拉起（挂起） ----
    let launched = match inject::spawn_suspended(&game, &st.cfg.game.game_args) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[FAIL] 启动游戏失败: {e}");
            return 1;
        }
    };
    println!("[5/6] 游戏已挂起启动 pid={}", launched.pid);

    // ---- 共享内存 ----
    let writer = match BridgeWriter::create(launched.pid) {
        Ok(w) => w,
        Err(e) => {
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            eprintln!("[FAIL] 共享内存创建失败: {e}");
            return 1;
        }
    };
    let payload = env.to_payload();
    println!("     bridge = {} ({} 字节)", writer.name, payload.len());
    if let Err(e) = writer.publish(&payload) {
        eprintln!("[FAIL] 写入共享内存失败: {e}");
    }
    // 同时落盘兜底：共享内存会随启动器进程退出而销毁，文件不会。
    match game.parent().map(|d| oo_shared::write_disk_payload(d, &payload)) {
        Some(Ok(p)) => println!("     磁盘兜底 = {} ({} 字节)", p.display(), payload.len()),
        Some(Err(e)) => eprintln!("[WARN] 写磁盘兜底失败: {e}"),
        None => eprintln!("[WARN] 无法确定游戏目录，跳过磁盘兜底"),
    }

    // ---- 注入（start -> resume -> finish，顺序不可颠倒）----
    let pending = match inject::inject_start(launched.process, &dll) {
        Ok(p) => p,
        Err(e) => {
            log_error!("inject start failed: {e}");
            let _ = inject::resume(launched.thread);
            inject::close(&launched);
            eprintln!("[FAIL] 注入准备失败: {e}");
            return 1;
        }
    };

    if let Err(e) = inject::resume(launched.thread) {
        log_error!("resume failed: {e}");
        eprintln!("[WARN] resume 失败: {e}");
    }

    if let Err(e) = inject::inject_finish(pending) {
        log_error!("inject finish failed: {e}");
        inject::close(&launched);
        eprintln!("[FAIL] 注入失败: {e}");
        return 1;
    }
    log_info!("injected: {}", dll.display());
    println!("     已注入 {}", dll.display());
    println!("[6/6] 游戏已放行。请在游戏内点击登录按钮。");
    println!("      注入日志: {}/oo_internal.log", dll.parent().unwrap().display());

    if st.cfg.game.keep_alive {
        let raw = inject::process_raw(&launched);
        std::thread::spawn(move || {
            let h = windows::Win32::Foundation::HANDLE(raw as *mut core::ffi::c_void);
            let code = inject::wait_exit(h);
            log_info!("game exited with code {code}");
            println!("游戏已退出，返回码 {code}");
        });
    }

    std::mem::forget(writer);
    std::mem::forget(launched);
    0
}