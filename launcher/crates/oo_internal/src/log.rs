//! DLL 内日志：写入 DLL 自身所在目录下的 oo_internal.log，同时 OutputDebugStringA。
//!
//! ★ 关键点：不能用 std::env::current_exe() —— 在 DLL 里它返回的是**宿主进程**的
//!   exe 路径（游戏 exe），不是 DLL 自己的路径。必须用 GetModuleFileNameW 传入
//!   本 DLL 的 HINSTANCE（DllMain 记录的），才能拿到 DLL 自身位置。

use std::ffi::c_void;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

static LOG: OnceLock<Mutex<Option<std::fs::File>>> = OnceLock::new();
static LOG_PATH: OnceLock<PathBuf> = OnceLock::new();

/// DllMain 记录的模块句柄，用于定位 DLL 自身路径。
pub static mut MODULE: *mut c_void = std::ptr::null_mut();

/// 取本 DLL 的完整路径。
fn module_path() -> Option<PathBuf> {
    use windows::Win32::Foundation::HMODULE;
    use windows::Win32::System::LibraryLoader::GetModuleFileNameW;

    let h = unsafe {
        let p = &raw const MODULE;
        HMODULE(*p)
    };
    let mut buf = vec![0u16; 1024];
    let n = unsafe { GetModuleFileNameW(Some(h), &mut buf) };
    if n == 0 {
        return None;
    }
    let s = String::from_utf16_lossy(&buf[..n as usize]);
    let p = PathBuf::from(s);
    tracing_note(&format!("module path resolved: {}", p.display()));
    Some(p)
}

/// 早期诊断：在日志系统就绪前，把信息写到 OutputDebugString + 一个固定位置的文件。
fn tracing_note(msg: &str) {
    output_debug_string(&format!("[OO_Internal] {msg}\n"));
}

pub fn init() {
    // 1) 先确定 DLL 自身目录
    let path = module_path()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
        .join("oo_internal.log");
    let _ = LOG_PATH.set(path.clone());

    // 2) 打开文件，并把失败原因送进 OutputDebugString
    let f = match OpenOptions::new().create(true).append(true).open(&path) {
        Ok(f) => Some(f),
        Err(e) => {
            tracing_note(&format!("FAILED to open {}: {e}", path.display()));
            None
        }
    };
    let _ = LOG.set(Mutex::new(f));

    write(&format!(
        "=== OO_Internal loaded (pid {}) log={} ===",
        std::process::id(),
        path.display()
    ));
}

pub fn write(msg: &str) {
    let stamp = timestamp();
    let line = format!("[{stamp}] {msg}");
    output_debug_string(&format!("[OO_Internal] {line}\n"));

    if let Some(m) = LOG.get() {
        match m.lock() {
            Ok(mut g) => {
                if let Some(f) = g.as_mut() {
                    let _ = f.write_all(line.as_bytes());
                    let _ = f.write_all(b"\n");
                    let _ = f.flush();
                }
            }
            Err(e) => {
                output_debug_string(&format!("[OO_Internal] log lock poisoned: {e}\n"));
            }
        }
    }
}

/// 日志文件路径（供自检/诊断读取）。
pub fn path() -> Option<&'static PathBuf> {
    LOG_PATH.get()
}

fn timestamp() -> String {
    use windows::Win32::System::SystemInformation::GetLocalTime;
    let st = unsafe { GetLocalTime() };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds
    )
}

fn output_debug_string(s: &str) {
    #[link(name = "kernel32")]
    extern "system" {
        fn OutputDebugStringA(lpoutputstring: *const u8);
    }
    let mut buf = s.as_bytes().to_vec();
    buf.push(0);
    unsafe { OutputDebugStringA(buf.as_ptr()) };
}
