//! 游戏进程拉起 + DLL 注入。
//!
//! 流程（与闭源私服 Launcher 行为等价，但目标服务器完全由我们自己决定）：
//!   1. CreateProcessW 以 CREATE_SUSPENDED 挂起方式启动 GuiLongchao.exe
//!   2. 为子进程 PID 创建共享内存区块并写入登录结果（由调用方完成）
//!   3. VirtualAllocEx 在子进程分配路径缓冲区 + WriteProcessMemory
//!   4. 取 LoadLibraryW 地址作为远程线程入口，CreateRemoteThread
//!   5. ResumeThread 放行主线程

use std::ffi::c_void;
use std::path::Path;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Diagnostics::Debug::WriteProcessMemory;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Memory::{
    VirtualAllocEx, VirtualFreeEx, MEM_COMMIT, MEM_RELEASE, MEM_RESERVE, PAGE_READWRITE,
};
use windows::Win32::System::Threading::{
    CreateProcessW, CreateRemoteThread, GetExitCodeProcess, GetExitCodeThread, ResumeThread,
    WaitForSingleObject, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, INFINITE,
    PROCESS_INFORMATION, STARTUPINFOW,
};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub struct Launched {
    pub pid: u32,
    pub process: HANDLE,
    pub thread: HANDLE,
}

unsafe impl Send for Launched {}

/// 以挂起方式启动游戏进程。
pub fn spawn_suspended(exe: &Path, args: &str) -> Result<Launched, String> {
    let dir = exe.parent().ok_or("game exe has no parent dir")?;
    let mut cmdline = format!("\"{}\"", exe.display());
    if !args.trim().is_empty() {
        cmdline.push(' ');
        cmdline.push_str(args.trim());
    }
    let mut cmd: Vec<u16> = cmdline.encode_utf16().chain(std::iter::once(0)).collect();
    let wdir = wide(&dir.to_string_lossy());

    let si = STARTUPINFOW {
        cb: std::mem::size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut pi = PROCESS_INFORMATION::default();

    unsafe {
    // ★ 2026-10-24：把补丁绕过开关显式传给子进程（子进程继承本进程环境）。
    //   DLL 侧默认也是开（见 oo_internal/lib.rs），这里是双保险：即使将来改默认值，
    //   启动器拉起的游戏也一定带上这个开关。用户可用 OO_PATCH_BYPASS=0 关闭。
    if std::env::var("OO_PATCH_BYPASS").is_err() {
        // SAFETY: 单线程启动阶段，早于 CreateProcessW，无并发读环境变量。
        unsafe { std::env::set_var("OO_PATCH_BYPASS", "1") };
    }
        CreateProcessW(
            PCWSTR::null(),
            Some(windows::core::PWSTR(cmd.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
            None,
            PCWSTR(wdir.as_ptr()),
            &si,
            &mut pi,
        )
        .map_err(|e| format!("CreateProcessW failed: {e}"))?;
    }

    Ok(Launched { pid: pi.dwProcessId, process: pi.hProcess, thread: pi.hThread })
}

/// 一次注入的中间态：远程线程已创建，等待放行主线程后再确认结果。
pub struct PendingInject {
    pub thread: HANDLE,
    pub remote: *mut c_void,
    pub process: HANDLE,
}

unsafe impl Send for PendingInject {}

/// 第一步：在挂起的进程里分配内存、写入 DLL 路径、创建远程 LoadLibraryW 线程。
///
/// ★ 关键：**此时不能等待远程线程**。
///   目标进程主线程仍处于挂起状态（CREATE_SUSPENDED），
///   若在此处 WaitForSingleObject，会一直等到超时（实测 15s 后失败），
///   表现为"注入失败"但进程随后被放行、DLL 却从未加载。
///   正确顺序：start → resume(主线程) → finish(等待并校验返回值)。
pub fn inject_start(process: HANDLE, dll_path: &Path) -> Result<PendingInject, String> {
    let dll_abs = dll_path
        .canonicalize()
        .map_err(|e| format!("inject dll not found: {} ({e})", dll_path.display()))?;
    let dll_str = dll_abs.to_string_lossy().to_string();
    let bytes: Vec<u16> = dll_str.encode_utf16().chain(std::iter::once(0)).collect();
    let nbytes = bytes.len() * 2;

    unsafe {
        let remote = VirtualAllocEx(process, None, nbytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if remote.is_null() {
            return Err("VirtualAllocEx failed".into());
        }

        let mut written = 0usize;
        if let Err(e) = WriteProcessMemory(
            process,
            remote,
            bytes.as_ptr() as *const c_void,
            nbytes,
            Some(&mut written),
        ) {
            let _ = VirtualFreeEx(process, remote, 0, MEM_RELEASE);
            return Err(format!("WriteProcessMemory failed: {e}"));
        }
        if written != nbytes {
            let _ = VirtualFreeEx(process, remote, 0, MEM_RELEASE);
            return Err(format!("short write: {written}/{nbytes}"));
        }

        let k32 = GetModuleHandleW(windows::core::w!("kernel32.dll"))
            .map_err(|e| format!("GetModuleHandleW(kernel32) failed: {e}"))?;
        let proc = GetProcAddress(k32, windows::core::s!("LoadLibraryW"))
            .ok_or("GetProcAddress(LoadLibraryW) failed")?;

        // LoadLibraryW 与 LPTHREAD_START_ROUTINE 签名不同但调用约定一致（均为 Win64 ABI）。
        let start: windows::Win32::System::Threading::LPTHREAD_START_ROUTINE =
            std::mem::transmute(proc);

        let thread = match CreateRemoteThread(process, None, 0, start, Some(remote), 0, None) {
            Ok(t) => t,
            Err(e) => {
                let _ = VirtualFreeEx(process, remote, 0, MEM_RELEASE);
                return Err(format!("CreateRemoteThread failed: {e}"));
            }
        };

        Ok(PendingInject { thread, remote, process })
    }
}

/// 第二步：等远程 LoadLibraryW 线程结束，并检查其返回值。
///
/// 远程线程的退出码就是 LoadLibraryW 返回的 HMODULE —— **0 表示加载失败**。
/// 只判断"线程结束了"是不够的：DLL 依赖缺失、架构不符、被杀软拦截，
/// 线程都会正常结束但返回 0。
pub fn inject_finish(p: PendingInject) -> Result<(), String> {
    unsafe {
        let wait = WaitForSingleObject(p.thread, 20_000);
        let mut exit_code = 0u32;
        let _ = GetExitCodeThread(p.thread, &mut exit_code);
        let _ = CloseHandle(p.thread);
        let _ = VirtualFreeEx(p.process, p.remote, 0, MEM_RELEASE);

        if wait != WAIT_OBJECT_0 {
            return Err(format!("LoadLibraryW remote thread did not finish (wait={wait:?})"));
        }
        if exit_code == 0 {
            return Err("LoadLibraryW returned NULL (DLL load failed in target process)".into());
        }
        Ok(())
    }
}

/// 放行主线程。
pub fn resume(thread: HANDLE) -> Result<(), String> {
    let n = unsafe { ResumeThread(thread) };
    if n == u32::MAX {
        return Err("ResumeThread failed".into());
    }
    Ok(())
}

/// 等待进程退出并返回退出码。
pub fn wait_exit(process: HANDLE) -> u32 {
    unsafe {
        let _ = WaitForSingleObject(process, INFINITE);
        let mut code = 0u32;
        let _ = GetExitCodeProcess(process, &mut code);
        code
    }
}

/// 关闭进程/线程句柄。
pub fn close(l: &Launched) {
    unsafe {
        let _ = CloseHandle(l.thread);
        let _ = CloseHandle(l.process);
    }
}

/// 只取进程句柄的裸值，供跨线程等待使用（句柄本身保持有效，由 Launched 持有）。
pub fn process_raw(l: &Launched) -> isize {
    l.process.0 as isize
}
