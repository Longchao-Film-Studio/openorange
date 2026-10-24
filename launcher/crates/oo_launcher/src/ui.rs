//! 主窗口、消息循环与业务编排。

use crate::config::Config;
use crate::flow::Reporter;
use crate::inject;
use crate::log::{self, Level};
use crate::net;
use crate::win32::{self, Controls};
use crate::{log_info, log_warn};
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
const ID_AUTO: i32 = 1007;

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
    h_auto: HWND,
    /// 「自动进入游戏」的初始勾选状态（由命令行 --auto 传入）。
    auto_enter: bool,
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
            h_auto: HWND::default(),
            auto_enter: false,
            busy: false,
        });
    }
}

/// 图形界面模式。
///
/// `auto` = 启动后是否自动进入游戏（等价于命令行模式的 --auto）：
///   勾上 -> 注入 DLL 时 OO_AUTO=1，客户端拿到凭证后直接进街区；
///   不勾 -> OO_AUTO=0，劫持照常（登录走我们后端），
///           但停在客户端自己的登录界面，由玩家手动点「开始游戏」。
pub fn run(auto: bool) {
    init_shared(false);
    state().auto_enter = auto;

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

    st.h_remember = c.checkbox(hwnd, "记住账号", ID_REMEMBER, 20, 118, 110, 22).unwrap();

    // ★ 自动进入游戏：
    //   勾上 = 注入时 OO_AUTO=1，拿到凭证后直接 EnterGame()，一路进街区；
    //   不勾 = OO_AUTO=0，登录照样劫持（走我们后端），
    //          但停在客户端自己的登录界面，玩家手动点「开始游戏」。
    st.h_auto = c.checkbox(hwnd, "自动进入游戏", ID_AUTO, 140, 118, 130, 22).unwrap();
    win32::set_check(st.h_auto, st.auto_enter);

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

/// flow::Reporter 的 GUI 实现：状态栏 + msgbox。
struct GuiReporter { hwnd: HWND }

impl crate::flow::Reporter for GuiReporter {
    fn status(&self, msg: &str) {
        set_status(msg);
        let _ = self.hwnd;
    }
    fn fail(&self, title: &str, msg: &str) {
        let h = self.hwnd;
        msgbox(h, msg, title, MB_OK | MB_ICONERROR);
        set_status(title);
        set_busy(false);
    }
    fn info(&self, msg: &str) {
        log_info!("{msg}");
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

    set_busy(true);
    let rep = GuiReporter { hwnd };
    match crate::flow::login_and_inject(&st.cfg.clone(), &username, &password,
                                        win32::get_check(st.h_auto), &rep) {
        crate::flow::FlowOutcome::Ok(writer, launched) => {
            // 保持映射与进程句柄存活到启动器退出
            crate::flow::keep_alive(writer, launched);
        }
        crate::flow::FlowOutcome::Failed(m) => {
            rep.fail("启动失败", &m);
        }
    }
}
// ============================================================================
// 命令行编排：与 on_login_clicked 完全相同的步骤，只是输入来自 argv。
// 用途：在无法操作 GUI 的环境下跑通完整流程（登录 -> 拉起 -> 注入 -> 放行）。
// 每个失败点都会明确打印，绝不静默。
// ============================================================================
/// flow::Reporter 的 CLI 实现。
struct CliReporter;

impl crate::flow::Reporter for CliReporter {
    fn status(&self, msg: &str) { println!("[*] {msg}"); }
    fn fail(&self, title: &str, msg: &str) { eprintln!("[FAIL] {title}: {msg}"); }
    fn info(&self, msg: &str) { println!("{msg}"); }
}

pub fn run_cli(username: &str, password: &str, auto: bool) -> i32 {
    init_shared(true);
    let st = state();
    log_info!("=== 命令行模式：{username}（auto={auto}）===");
    let rep = CliReporter;
    match crate::flow::login_and_inject(&st.cfg.clone(), username, password, auto, &rep) {
        crate::flow::FlowOutcome::Ok(writer, launched) => {
            let pid = launched.pid;
            let dll = st.cfg.resolve_inject_dll();
            println!("[6/6] 游戏已放行（pid={pid}）。请在游戏内点击登录按钮。");
            println!("      注入日志: {}/oo_internal.log", dll.parent().unwrap().display());
            // 流程要求句柄存活：CLI 模式进程可能立即退出，keep_alive 用 forget 保持
            // （keep_alive 标志下已有退出监控线程，由 flow 内部管理）
            let raw = inject::process_raw(&launched);
            std::thread::spawn(move || {
                let h = windows::Win32::Foundation::HANDLE(raw as *mut core::ffi::c_void);
                let code = inject::wait_exit(h);
                println!("游戏已退出，返回码 {code}");
            });
            std::mem::forget(writer);
            std::mem::forget(launched);
            0
        }
        crate::flow::FlowOutcome::Failed(m) => {
            eprintln!("[FAIL] {m}");
            1
        }
    }
}