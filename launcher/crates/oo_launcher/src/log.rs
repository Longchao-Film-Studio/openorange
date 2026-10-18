//! 极简日志：控制台 + 文件双写。发布版只写文件。

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level { Error = 0, Warn = 1, Info = 2, Debug = 3 }

impl Level {
    pub fn parse(s: &str) -> Level {
        match s.trim().to_ascii_lowercase().as_str() {
            "error" => Level::Error,
            "warn" | "warning" => Level::Warn,
            "debug" => Level::Debug,
            _ => Level::Info,
        }
    }
    fn tag(self) -> &'static str {
        match self {
            Level::Error => "ERROR",
            Level::Warn => "WARN " ,
            Level::Info => "INFO " ,
            Level::Debug => "DEBUG",
        }
    }
}

pub struct Logger {
    level: Level,
    to_console: bool,
    file: Option<Mutex<File>>,
}

static mut LOGGER: Option<Logger> = None;

/// 安全地取得全局 logger（启动器为单线程消息循环，访问点固定在主线程）。
fn logger() -> &'static Logger {
    unsafe {
        let p = &raw const LOGGER;
        match &*p {
            Some(l) => l,
            None => {
                // 未初始化时退化为一个只写控制台的默认实例
                let p = &raw mut LOGGER;
                *p = Some(Logger { level: Level::Info, to_console: true, file: None });
                (*p).as_ref().unwrap()
            }
        }
    }
}

pub fn init(level: Level, to_console: bool, log_path: Option<PathBuf>) {
    let file = log_path.and_then(|p| {
        if let Some(dir) = p.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        OpenOptions::new().create(true).append(true).open(&p).ok()
    });
    let l = Logger { level, to_console, file: file.map(Mutex::new) };
    unsafe {
        let p = &raw mut LOGGER;
        *p = Some(l);
    }
}

pub fn write(level: Level, msg: &str) {
    let lg = logger();
    if level > lg.level { return; }
    let stamp = timestamp();
    let line = format!("[{stamp}] {} {msg}", level.tag());
    if lg.to_console {
        let _ = writeln!(std::io::stdout(), "{line}");
    }
    if let Some(f) = &lg.file {
        if let Ok(mut f) = f.lock() {
            let _ = writeln!(f, "{line}");
            let _ = f.flush();
        }
    }
}

fn timestamp() -> String {
    use windows::Win32::System::SystemInformation::GetLocalTime;
    let st = unsafe { GetLocalTime() };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}",
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, st.wMilliseconds
    )
}

#[macro_export]
macro_rules! log_error { ($($a:tt)*) => { $crate::log::write($crate::log::Level::Error, &format!($($a)*)) }; }
#[macro_export]
macro_rules! log_warn  { ($($a:tt)*) => { $crate::log::write($crate::log::Level::Warn,  &format!($($a)*)) }; }
#[macro_export]
macro_rules! log_info  { ($($a:tt)*) => { $crate::log::write($crate::log::Level::Info,  &format!($($a)*)) }; }
#[macro_export]
macro_rules! log_debug { ($($a:tt)*) => { $crate::log::write($crate::log::Level::Debug, &format!($($a)*)) }; }
