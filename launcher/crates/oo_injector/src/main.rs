//! 命令行注入器 —— 复用 OO_Launcher 完全相同的注入路径，
//! 用于在**不启动游戏**的情况下验证注入链路。
//!
//! 用法: oo_injector <pid> <dll_path> [account_id] [ticket]

use std::ffi::c_void;
use oo_shared::{channel_name, BridgeHeader, BRIDGE_CAPACITY, BRIDGE_MAGIC, BRIDGE_VERSION, LoginEnvelope, STATE_READY};
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows::Win32::System::Diagnostics::Debug::WriteProcessMemory;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Memory::{
    CreateFileMappingW, MapViewOfFile, VirtualAllocEx, MEM_COMMIT, MEM_RELEASE, MEM_RESERVE,
    PAGE_READWRITE,
};
use windows::Win32::System::Threading::{
    CreateRemoteThread, OpenProcess, WaitForSingleObject, PROCESS_ALL_ACCESS,
};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();

    // ---- probe 模式：读取指定 pid 的共享内存，验证启动器是否写入成功 ----
    if args.len() >= 2 && args[1] == "--probe" {
        let pid2: u32 = args[2].parse().expect("pid");
        let name = channel_name(pid2);
        println!("probe channel = {name}");
        let wname = wide(&name);
        unsafe {
            let m = windows::Win32::System::Memory::OpenFileMappingW(
                windows::Win32::System::Memory::FILE_MAP_ALL_ACCESS.0,
                false,
                PCWSTR(wname.as_ptr()),
            );
            match m {
                Ok(m) => {
                    println!("OpenFileMappingW OK");
                    let v = MapViewOfFile(
                        m,
                        windows::Win32::System::Memory::FILE_MAP_ALL_ACCESS,
                        0,
                        0,
                        BRIDGE_CAPACITY,
                    );
                    if v.Value.is_null() {
                        println!("MapViewOfFile FAILED");
                        return;
                    }
                    let base = v.Value as *const u8;
                    let h = &*(base as *const BridgeHeader);
                    println!("magic   = 0x{:08X} (expect 0x{:08X})", h.magic, BRIDGE_MAGIC);
                    println!("version = {} (expect {})", h.version, BRIDGE_VERSION);
                    println!("state   = {} (expect {})", h.state, STATE_READY);
                    println!("length  = {}", h.length);
                    let pdata = base.add(std::mem::size_of::<BridgeHeader>());
                    let n = (h.length as usize).min(2048);
                    let s = std::slice::from_raw_parts(pdata, n);
                    println!("payload = {}", String::from_utf8_lossy(s));
                }
                Err(e) => println!("OpenFileMappingW FAILED: {e}"),
            }
        }
        return;
    }

    if args.len() < 3 {
        eprintln!("用法: oo_injector <pid> <dll_path> [account_id] [ticket]");
        std::process::exit(1);
    }
    let pid: u32 = args[1].parse().expect("pid 必须是数字");
    let dll = args[2].clone();
    let account_id = args.get(3).cloned().unwrap_or_else(|| "oo_hosttest_acct".into());
    let ticket = args.get(4).cloned().unwrap_or_else(|| "hosttest_ticket".into());

    println!("=== oo_injector ===");
    println!("target pid = {pid}");
    println!("dll        = {dll}");

    // ---- 1) 共享内存 ----
    let env = LoginEnvelope::ok(serde_json::json!({
        "account_id": account_id,
        "ticket": ticket,
        "channel_uid": "556938146",
        "is_new": false,
        "is_formal": true,
    }));
    let payload = env.to_payload();

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
        .expect("CreateFileMappingW failed");
        let view = MapViewOfFile(mapping, windows::Win32::System::Memory::FILE_MAP_ALL_ACCESS, 0, 0, BRIDGE_CAPACITY);
        assert!(!view.Value.is_null());
        let base = view.Value as *mut u8;

        let h = BridgeHeader::default();
        std::ptr::copy_nonoverlapping(&h as *const _ as *const u8, base, std::mem::size_of::<BridgeHeader>());
        let pdata = base.add(std::mem::size_of::<BridgeHeader>());
        std::ptr::copy_nonoverlapping(payload.as_ptr(), pdata, payload.len());
        let hh = &mut *(base as *mut BridgeHeader);
        hh.magic = BRIDGE_MAGIC;
        hh.version = BRIDGE_VERSION;
        hh.length = payload.len() as u64;
        hh.state = STATE_READY;
        println!("bridge ready: {name} ({} bytes)", payload.len());

        // ---- 2) 打开目标进程 ----
        let proc = OpenProcess(PROCESS_ALL_ACCESS, false, pid).expect("OpenProcess failed");
        println!("OpenProcess OK");

        // ---- 3) 远程分配 + 写路径 ----
        let abs = std::fs::canonicalize(&dll).expect("dll not found");
        let dll_str = abs.to_string_lossy().to_string();
        let bytes: Vec<u16> = dll_str.encode_utf16().chain(std::iter::once(0)).collect();
        let nbytes = bytes.len() * 2;
        let remote = VirtualAllocEx(proc, None, nbytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        assert!(!remote.is_null(), "VirtualAllocEx failed");
        let mut written = 0usize;
        WriteProcessMemory(proc, remote, bytes.as_ptr() as *const c_void, nbytes, Some(&mut written))
            .expect("WriteProcessMemory failed");
        println!("WriteProcessMemory OK ({written}/{nbytes} bytes)");

        // ---- 4) LoadLibraryW ----
        let k32 = GetModuleHandleW(windows::core::w!("kernel32.dll")).unwrap();
        let ll = GetProcAddress(k32, windows::core::s!("LoadLibraryW")).expect("LoadLibraryW not found");
        let start: windows::Win32::System::Threading::LPTHREAD_START_ROUTINE = std::mem::transmute(ll);
        let th = CreateRemoteThread(proc, None, 0, start, Some(remote), 0, None).expect("CreateRemoteThread failed");
        let w = WaitForSingleObject(th, 15000);
        println!("remote LoadLibraryW finished (wait={w:?})");
        let _ = CloseHandle(th);
        let _ = windows::Win32::System::Memory::VirtualFreeEx(proc, remote, 0, MEM_RELEASE);
        let _ = CloseHandle(proc);
        println!("=== injection done ===");
    }
}
