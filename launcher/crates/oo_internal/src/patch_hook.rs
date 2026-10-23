//! HybridLoadMgr 补丁下载兜底（队列任务：官方 URL 依赖，方案 A）。
//!
//! ── 背景（2026-10-23 实机取证，见 task-6 与 docs/整改清单 §11·补）──────────
//!
//! 客户端启动时 HybridLoad.HybridLoadMgr::DownloadPatchInfo 强依赖官方补丁服：
//!   * 失败形态 "Cannot resolve destination host"（DNS 解析不了）
//!       -> 回落读本地 PersistentPath/HybridData/patch_info.json
//!       -> "PatchInfo Download success" -> CheckVersion(本地 VersionInfo) -> 能进游戏。
//!   * 失败形态 "Cannot connect to destination host"（能解析但 TCP 连不通，
//!     典型：全局 TUN 断网 / 坏代理 / 官方半死）
//!       -> HybridLoadMgr.ShowError -> 循环重试 -> 进不去。
//!
//! ── 为什么 hook <DownloadPatchInfo>b__0 ─────────────────────────────────
//!
//! il2cpp 静态私有方法的函数体普遍以 `cmp byte ptr [rip+xxx],0` 开头
//! （il2cpp 运行时初始化检查），EnterNextState(0x17F7AB0) / DownloadPatchInfo(0x17F7600)
//! 的序言均含 RIP 相对寻址，复制到 trampoline 后基址改变 -> 宿主崩溃
//! （与 xlua.rs 头注释 luaL_loadbuffer 的情况同一类），precheck 会正确拒绝。
//!
//! 全 HybridLoad 命名空间扫描后，<DownloadPatchInfo>b__0（RVA 0x1801520）是
//! 唯一与补丁下载失败相关且序言 24 字节内无 RIP 相对、无相对跳转的可挂钩点：
//!   48 89 5c 24 18 48 89 4c 24 08 56 57 41 54 41 56 ...
//!
//! b__0 是 DownloadPatchInfo 交给 DownFile 的 onComplete 回调（Ghidra 反编译实证）：
//!   日志实锤（2026.1022.205003.txt）：
//!     webrequest ... error: Cannot connect to destination host
//!     HybridLoad.<>c__DisplayClass51_0:<DownloadPatchInfo>b__0(String)  <- 崩在这
//!     EnterNextState ShowError
//! b__0(this, error)：error!=0 -> 记错误码 2 + return；error==0 -> 走成功路径。
//! 因此 detour 把 RDX(error) 置 null 后转发 trampoline —— 强制走成功路径，
//! 用本地缓存拼出 mRemote* 字段（与 "Cannot resolve" 形态同效，已实测可进游戏）。
//!
//! ── 地址解析（为什么是 RVA 而不是 GetProcAddress）────────────────────────
//!
//! 磁盘上的 GameAssembly.dll(21MB) 是加密/加壳形态（节表 RawPtr 全 0、
//! 无 il2cpp 导出、无任何符号字符串）；运行时解密展开为 77MB 镜像，节 VA 不变。
//! 因此：运行时 GetModuleHandleW("GameAssembly.dll") 基址 + RVA 即目标地址。
//! RVA 来自 Il2CppDumper（输入 = archive/reverse/il2cpp_clean 的 77MB 运行时 dump
//! + 同批解密 metadata），已验证 RVA 处字节为标准 x64 函数序言。

use crate::log;
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;

/// GameAssembly.dll 里 <DownloadPatchInfo>b__0 的 RVA。
/// 依据：Il2CppDumper script.json（dump 对象 = il2cpp_clean 77MB 运行时镜像）。
const RVA_B0: usize = 0x0180_1520;
/// 预期序言（前 16 字节）。安装前必须逐字节匹配，防止 dump 与实机版本不符时写歪。
const EXPECT_PROLOGUE: [u8; 16] = [
    0x48, 0x89, 0x5c, 0x24, 0x18, // mov [rsp+18h],rbx
    0x48, 0x89, 0x4c, 0x24, 0x08, // mov [rsp+8],rcx
    0x56, 0x57,                   // push rsi / push rdi
    0x41, 0x54, 0x41, 0x56,       // push r12 / push r14
];

static INSTALLED: AtomicBool = AtomicBool::new(false);
static SWALLOWED: AtomicUsize = AtomicUsize::new(0);
static TRAMP: std::sync::atomic::AtomicPtr<c_void> =
    std::sync::atomic::AtomicPtr::new(std::ptr::null_mut());

pub fn is_installed() -> bool {
    INSTALLED.load(Ordering::SeqCst)
}

pub fn swallowed_count() -> usize {
    SWALLOWED.load(Ordering::SeqCst)
}

/// b__0 的 il2cpp AOT 签名：fn(this=<DisplayClass51_0*>, error=System.String*, MethodInfo*)。
/// Win64 ABI：RCX=this, RDX=error, R9=MethodInfo（第三参按 il2cpp 惯例在 R8/R9 视实现而定，
/// 我们不碰参数，只吞调用，因此不需要精确知道参数位次）。
type B0Fn = unsafe extern "C" fn(*mut c_void, *mut c_void) -> ();

unsafe extern "C" fn b0_detour(this: *mut c_void, _error: *mut c_void) {
    // ★ 实测（2026-10-24 02:28/02:43 两次）：b__0 的「成功分支」内部会执行
    //   File.Move(patch_info.json.temp → patch_info.json)，而 temp 只在真实下载成功时存在。
    //   下载失败被我们拦截时 temp 不存在 → FileNotFoundException → 照样 ShowError。
    //   修复：进入成功分支前，把本地 patch_info.json 复制为 .temp（内容正确，Move 后等价回落本地）。
    prepare_temp_file();
    let n = SWALLOWED.fetch_add(1, Ordering::SeqCst);
    if n == 0 {
        log::write("patch_hook: b__0 bypass active (nulling error arg)");
    }
    // ★ 反编译实证（ghidra patch_funcs_dec.c / DownloadPatchInfo_b__0）：
    //     if (error != 0) {
    //         if (this->field_0x10 != 0) {
    //             FUN_xxx(this->field_0x10, error, 2);   // 2 = HybridLoadError.DownloadPatchInfo
    //             return;                                //   -> 就是弹「错误码2」的那条路
    //         }
    //         ... 不可恢复错误路径（does not return）
    //     }
    //     <== error == 0 时才会走下面的成功逻辑：拼 mRemoteChannelDirUrl(+0x88/+0x90)
    // 因此正确做法不是吞调用，而是【把 error 改成 null 再转发】，
    // 让它走成功路径、用本地缓存拼出 mRemote* 字段（与 "Cannot resolve" 形态同效）。
    let error: *mut c_void = std::ptr::null_mut();
    let tramp = TRAMP.load(Ordering::SeqCst);
    if tramp.is_null() {
        log::write("patch_hook: FATAL trampoline missing");
        return;
    }
    let real: B0Fn = std::mem::transmute(tramp);
    real(this, std::ptr::null_mut());
}

/// 把本地 patch_info.json 复制为 patch_info.json.temp（若 temp 不存在）。
/// 路径与客户端一致：{PersistentPath}/HybridData/patch_info.json(.temp)。
/// PersistentPath 无法在 native 侧直接拿到（Unity 内部值），用客户端日志实证的绝对路径：
/// 游戏目录相对路径 —— 通过 GameAssembly 所在目录推导：../GuiLongchao_Data/PersistentPath/HybridData。
fn prepare_temp_file() {
    use std::path::PathBuf;
    let Some(base) = game_assembly_base() else { return; };
    // GameAssembly.dll 的模块路径
    let mut buf = [0u16; 512];
    unsafe {
        let n = windows::Win32::System::LibraryLoader::GetModuleFileNameW(
            Some(windows::Win32::Foundation::HMODULE(base)),
            &mut buf,
        );
        if n == 0 { return; }
    }
    let exe_path = PathBuf::from(String::from_utf16_lossy(&buf[..buf.iter().position(|&c| c == 0).unwrap_or(0)]));
    // <game>/GameAssembly.dll -> <game>/GuiLongchao_Data/PersistentPath/HybridData
    let Some(game_dir) = exe_path.parent() else { return };
    let hyb = game_dir.join("GuiLongchao_Data").join("PersistentPath").join("HybridData");
    let json = hyb.join("patch_info.json");
    let temp = hyb.join("patch_info.json.temp");
    if json.exists() && !temp.exists() {
        match std::fs::copy(&json, &temp) {
            Ok(n) => log::write(&format!("patch_hook: seeded patch_info.json.temp ({} bytes)", n)),
            Err(e) => log::write(&format!("patch_hook: seed temp failed: {}", e)),
        }
    }
}

/// 取 GameAssembly.dll 模块基址。
fn game_assembly_base() -> Option<*mut c_void> {
    let w: Vec<u16> = "GameAssembly.dll".encode_utf16().chain(std::iter::once(0)).collect();
    unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())).ok() }
        .map(|h| h.0 as *mut c_void)
}

/// 尝试安装 hook。失败返回 Err（调用方可以继续轮询重试）。
pub fn try_install() -> Result<(), String> {
    if INSTALLED.load(Ordering::SeqCst) {
        return Ok(());
    }
    let base = game_assembly_base().ok_or("GameAssembly.dll not loaded yet")?;
    let target = (base as usize + RVA_B0) as *mut c_void;
    unsafe {
        let bytes = std::slice::from_raw_parts(target as *const u8, EXPECT_PROLOGUE.len());
        if bytes != EXPECT_PROLOGUE {
            return Err(format!("prologue mismatch at RVA {RVA_B0:#x}: {}",
                crate::xlua::hex_dump(target as *const u8, 16)));
        }
    }
    // 通用 inline hook（不触碰 xlua 的单例状态）。
    let tramp = crate::xlua::install_generic(target, b0_detour as *const c_void, "patch")?;
    TRAMP.store(tramp, Ordering::SeqCst);
    INSTALLED.store(true, Ordering::SeqCst);
    log::write(&format!(
        "patch_hook: installed at GameAssembly+{RVA_B0:#x} (b0 bypass armed)"
    ));
    Ok(())
}

/// 后台轮询安装入口：GameAssembly 就绪（模块出现且序言匹配）即装。
/// 由 lib.rs 的 bootstrap 线程在开关打开时调用。
pub fn spawn_wait_thread() {
    std::thread::spawn(|| {
        let mut waited = 0u32;
        while waited < 600_000 {
            match try_install() {
                Ok(()) => return,
                Err(e) => {
                    if waited % 10_000 == 0 {
                        log::write(&format!("patch_hook: waiting... {e}"));
                    }
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
            waited += 200;
        }
        log::write("patch_hook: gave up after 600s (GameAssembly prologue never matched)");
    });
}
