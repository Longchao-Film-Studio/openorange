//! 共享内存读端：读取启动器为"本进程 PID"创建的登录结果。
//!
//! 惰性打开 —— 首次调用 PC_Internal_Result 时才 OpenFileMapping。

use oo_shared::{channel_name, BridgeHeader, BRIDGE_MAGIC, BRIDGE_VERSION, STATE_READY};
use std::sync::{Mutex, OnceLock};
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::Memory::{OpenFileMappingW, MapViewOfFile, FILE_MAP_READ};

struct View {
    _mapping: HANDLE,
    base: *const u8,
}

unsafe impl Send for View {}

static VIEW: OnceLock<Mutex<Option<View>>> = OnceLock::new();

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn slot() -> &'static Mutex<Option<View>> {
    VIEW.get_or_init(|| Mutex::new(None))
}

fn ensure_open() -> bool {
    let mut g = match slot().lock() {
        Ok(g) => g,
        Err(_) => return false,
    };
    if g.is_some() {
        return true;
    }
    let pid = std::process::id();
    let name = channel_name(pid);
    let wname = wide(&name);
    unsafe {
        let mapping = OpenFileMappingW(FILE_MAP_READ.0, false, PCWSTR(wname.as_ptr()));
        let Ok(mapping) = mapping else {
            return false;
        };
        if mapping.is_invalid() {
            return false;
        }
        let view = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
        if view.Value.is_null() {
            let _ = CloseHandle(mapping);
            return false;
        }
        *g = Some(View { _mapping: mapping, base: view.Value as *const u8 });
        true
    }
}

/// 磁盘兜底：从游戏可执行文件同目录读取 oo_login.json。
///
/// 为什么必须有：共享内存的生命周期绑定在创建它的进程上。启动器写入
/// Local\OO_InternalPC-<pid> 后一旦退出（CLI 模式跑完即退、或用户关掉窗口），
/// Windows 回收句柄 -> 映射对象销毁 -> 本 DLL 再 OpenFileMappingW 会得到
/// "系统找不到指定的文件 (0x80070002)"。
/// 实测：启动器写入成功（1095 字节），但游戏 36.6 秒后去读时映射已消失，
/// 导致 login result not ready。文件通道彻底摆脱该约束。
fn read_disk_fallback() -> String {
    let dir = match std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
    {
        Some(d) => d,
        None => return String::new(),
    };
    match oo_shared::read_disk_payload(&dir) {
        Some(s) if !s.is_empty() => {
            crate::log::write(&format!(
                "bridge: shared memory unavailable, using disk fallback ({} bytes)",
                s.len()
            ));
            s
        }
        _ => String::new(),
    }
}

/// 读取登录结果 JSON。未就绪时返回空串（与闭源实现语义一致）。
///
/// 读取顺序：共享内存 -> 磁盘兜底。
pub fn read_result() -> String {
    let from_shm = read_result_shm();
    if !from_shm.is_empty() {
        return from_shm;
    }
    read_disk_fallback()
}

fn read_result_shm() -> String {
    if !ensure_open() {
        return String::new();
    }
    let g = match slot().lock() {
        Ok(g) => g,
        Err(_) => return String::new(),
    };
    let Some(v) = g.as_ref() else {
        return String::new();
    };

    unsafe {
        let h = &*(v.base as *const BridgeHeader);
        if h.magic != BRIDGE_MAGIC || h.version != BRIDGE_VERSION {
            return String::new();
        }
        if h.state != STATE_READY {
            return String::new();
        }
        let len = h.length as usize;
        if len == 0 || len > oo_shared::BRIDGE_PAYLOAD_CAPACITY {
            return String::new();
        }
        let payload = v.base.add(std::mem::size_of::<BridgeHeader>());
        let slice = std::slice::from_raw_parts(payload, len);
        String::from_utf8_lossy(slice).to_string()
    }
}

/// 共享内存是否已经可读（自检用）。
pub fn is_available() -> bool {
    ensure_open()
}