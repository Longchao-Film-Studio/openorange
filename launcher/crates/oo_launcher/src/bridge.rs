//! 共享内存写端：由启动器创建命名的 64KB 区块，游戏进程内的 DLL 只读打开。
//!
//! 与闭源私服 InternalPC.dll 的 OpenFileMappingA 方案等价。
//! 注意：游戏进程创建于本区块之后，因此不存在竞态。

use oo_shared::{channel_name, BridgeHeader, BRIDGE_CAPACITY, BRIDGE_VERSION, STATE_PENDING, STATE_READY};
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows::Win32::System::Memory::{
    CreateFileMappingW, MapViewOfFile, UnmapViewOfFile, FILE_MAP_ALL_ACCESS, PAGE_READWRITE,
};

pub struct BridgeWriter {
    pub name: String,
    mapping: HANDLE,
    view: *mut u8,
}

unsafe impl Send for BridgeWriter {}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

impl BridgeWriter {
    /// 为指定游戏进程 PID 创建共享区块。
    pub fn create(pid: u32) -> Result<BridgeWriter, String> {
        let name = channel_name(pid);
        let wname = wide(&name);
        unsafe {
            let mapping = CreateFileMappingW(
                INVALID_HANDLE_VALUE,
                None,
                PAGE_READWRITE,
                (BRIDGE_CAPACITY >> 16) as u32,
                (BRIDGE_CAPACITY & 0xFFFF) as u32,
                PCWSTR(wname.as_ptr()),
            )
            .map_err(|e| format!("CreateFileMappingW failed: {e}"))?;

            if mapping.is_invalid() {
                return Err("CreateFileMappingW returned invalid handle".into());
            }

            let view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, BRIDGE_CAPACITY);
            if view.Value.is_null() {
                let _ = CloseHandle(mapping);
                return Err("MapViewOfFile failed".into());
            }

            let w = BridgeWriter { name, mapping, view: view.Value as *mut u8 };
            w.reset_header();
            Ok(w)
        }
    }

    fn reset_header(&self) {
        let h = BridgeHeader::default();
        unsafe {
            std::ptr::copy_nonoverlapping(
                &h as *const BridgeHeader as *const u8,
                self.view,
                std::mem::size_of::<BridgeHeader>(),
            );
        }
    }

    fn header_ptr(&self) -> *mut BridgeHeader {
        self.view as *mut BridgeHeader
    }

    fn payload_ptr(&self) -> *mut u8 {
        unsafe { self.view.add(std::mem::size_of::<BridgeHeader>()) }
    }

    /// 写入结果 JSON，并把 state 置为 ready。
    pub fn publish(&self, json: &str) -> Result<(), String> {
        let bytes = json.as_bytes();
        let cap = oo_shared::BRIDGE_PAYLOAD_CAPACITY;
        if bytes.len() > cap {
            return Err(format!("payload too large: {} > {}", bytes.len(), cap));
        }
        unsafe {
            // 先写数据，再发布长度，最后置 state —— 读端按 state 判定就绪。
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), self.payload_ptr(), bytes.len());
            let h = &mut *self.header_ptr();
            h.magic = oo_shared::BRIDGE_MAGIC;
            h.version = BRIDGE_VERSION;
            h.length = bytes.len() as u64;
            std::sync::atomic::fence(std::sync::atomic::Ordering::Release);
            h.state = STATE_READY;
        }
        Ok(())
    }

    /// 当前状态（自检用）。
    pub fn state(&self) -> u32 {
        unsafe { (*self.header_ptr()).state }
    }

    pub fn mark_pending(&self) {
        unsafe { (*self.header_ptr()).state = STATE_PENDING; }
    }
}

impl Drop for BridgeWriter {
    fn drop(&mut self) {
        unsafe {
            if !self.view.is_null() {
                let _ = UnmapViewOfFile(windows::Win32::System::Memory::MEMORY_MAPPED_VIEW_ADDRESS {
                    Value: self.view as *mut core::ffi::c_void,
                });
            }
            if !self.mapping.is_invalid() {
                let _ = CloseHandle(self.mapping);
            }
        }
    }
}
