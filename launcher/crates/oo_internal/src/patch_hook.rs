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

/// hook 目标表：HybridLoad 各阶段下载失败回调（b__0）。
/// 依据：Il2CppDumper script.json（dump 对象 = il2cpp_clean 77MB 运行时镜像），
/// 序言均在 24 字节内无 RIP 相对（2026-10-24 逐一验证）。
/// 实测推进：DownloadPatchInfo ✅ → CheckVersion 需要 hook（VersionInfo 下载失败同类）→
/// DownloadInfo / DownloadLuaInfo 预防性覆盖。
const HOOK_TARGETS: &[(usize, [u8; 12], &str)] = &[
    // (RVA, 预期序言前12字节, 标签)
    (0x0180_1520, [0x48, 0x89, 0x5c, 0x24, 0x18, 0x48, 0x89, 0x4c, 0x24, 0x08, 0x56, 0x57], "DownloadPatchInfo"),
    (0x0180_1AC0, [0x48, 0x89, 0x4c, 0x24, 0x08, 0x53, 0x56, 0x57, 0x41, 0x56, 0x41, 0x57], "CheckVersion"),
    (0x0180_24E0, [0x48, 0x89, 0x4c, 0x24, 0x08, 0x53, 0x48, 0x83, 0xec, 0x30, 0x48, 0x8b], "DownloadInfo"),
    (0x0180_2590, [0x48, 0x89, 0x4c, 0x24, 0x08, 0x53, 0x56, 0x57, 0x41, 0x56, 0x41, 0x57], "DownloadLuaInfo"),
];

static INSTALLED: AtomicBool = AtomicBool::new(false);
static INSTALLED_FLAGS: [AtomicBool; 8] = [
    AtomicBool::new(false), AtomicBool::new(false), AtomicBool::new(false), AtomicBool::new(false),
    AtomicBool::new(false), AtomicBool::new(false), AtomicBool::new(false), AtomicBool::new(false),
];
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
    let _error: *mut c_void = std::ptr::null_mut();
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
/// 播种 patch_info.json.temp（b__0 成功分支会 File.Delete(json) + File.Move(temp→json)）。
///
/// ★ 实测教训（2026-10-24 三次验收）：
///   1) 客户端成功路径会先 Delete 已存在的 json 再 Move —— 所以 json 每次启动都会被吃掉；
///   2) 因此不能依赖 json 存在，必须把正确内容（5740B 加密形态）内嵌进 DLL 无条件写 temp。
/// 内容来源：work/oo_analysis/cdn/patch_info_release.json（与原 PersistentPath 文件字节一致）。
fn prepare_temp_file() {
    use std::path::PathBuf;
    const PATCH_INFO: &[u8] = include_bytes!("data/patch_info.json");
    let Some(base) = game_assembly_base() else { return; };
    let mut buf = [0u16; 512];
    let n = unsafe {
        windows::Win32::System::LibraryLoader::GetModuleFileNameW(
            Some(windows::Win32::Foundation::HMODULE(base)),
            &mut buf,
        )
    };
    if n == 0 { return; }
    let exe_path = PathBuf::from(String::from_utf16_lossy(&buf[..buf.iter().position(|&c| c == 0).unwrap_or(0)]));
    let Some(game_dir) = exe_path.parent() else { return };
    let hyb = game_dir.join("GuiLongchao_Data").join("PersistentPath").join("HybridData");
    let temp = hyb.join("patch_info.json.temp");
    // 无条件写（客户端每次启动都会 Delete json，temp 必须每次都新鲜）
    match std::fs::write(&temp, PATCH_INFO) {
        Ok(()) => log::write(&format!("patch_hook: seeded patch_info.json.temp ({} bytes)", PATCH_INFO.len())),
        Err(e) => log::write(&format!("patch_hook: seed temp failed: {}", e)),
    }
}
/// 取 GameAssembly.dll 模块基址。
fn game_assembly_base() -> Option<*mut c_void> {
    let w: Vec<u16> = "GameAssembly.dll".encode_utf16().chain(std::iter::once(0)).collect();
    unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())).ok() }
        .map(|h| h.0 as *mut c_void)
}

/// 尝试安装全部 hook。每个目标独立校验序言；已装的总数返回。
pub fn try_install() -> Result<usize, String> {
    let base = game_assembly_base().ok_or("GameAssembly.dll not loaded yet")?;
    let mut ok = 0usize;
    let mut last_err = String::new();
    for (rva, expect, label) in HOOK_TARGETS {
        if INSTALLED_FLAGS[label_hash(label)].load(Ordering::SeqCst) { ok += 1; continue; }
        let target = (base as usize + rva) as *mut c_void;
        unsafe {
            let bytes = std::slice::from_raw_parts(target as *const u8, expect.len());
            if bytes != *expect {
                last_err = format!("prologue mismatch at {label} RVA {rva:#x}: {}",
                    crate::xlua::hex_dump(target as *const u8, 16));
                continue;
            }
        }
        match crate::xlua::install_generic(target, b0_detour as *const c_void, label) {
            Ok(_) => {
                INSTALLED_FLAGS[label_hash(label)].store(true, Ordering::SeqCst);
                ok += 1;
                log::write(&format!("patch_hook: {label} hook armed at GameAssembly+{rva:#x}"));
            }
            Err(e) => last_err = format!("{label}: {e}"),
        }
    }
    if ok == 0 {
        return Err(last_err);
    }
    if ok == HOOK_TARGETS.len() {
        INSTALLED.store(true, Ordering::SeqCst);
    }
    Ok(ok)
}

/// label -> 稳定小下标（目标少，直接线性查）。
fn label_hash(label: &str) -> usize {
    HOOK_TARGETS.iter().position(|(_, _, l)| *l == label).unwrap_or(0)
}
/// 后台轮询安装入口：GameAssembly 就绪（模块出现且序言匹配）即装。
/// 由 lib.rs 的 bootstrap 线程在开关打开时调用。
pub fn spawn_wait_thread() {
    std::thread::spawn(|| {
        let mut waited = 0u32;
        while waited < 600_000 {
            match try_install() {
                Ok(n) if n == HOOK_TARGETS.len() => return,
                Ok(_) => {}
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
