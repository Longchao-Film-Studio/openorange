//! OO_Internal.dll —— 归龙潮客户端内注入模块。
//!
//! 工作流程：
//!   1. 启动器以 CREATE_SUSPENDED 拉起游戏并 LoadLibrary 本 DLL；
//!   2. DllMain 中启动后台线程（不在 DllMain 里做重活，避免 loader lock 死锁）；
//!   3. 后台线程等待 xlua.dll 就绪，定位 luaL_loadbuffer 并安装内联跳转；
//!   4. detour 在 xlua 首次加载 Lua 模块前，把 OO_Internal_* 原生函数与引导脚本注入；
//!   5. 引导脚本接管 platform/login，把启动器投递的身份喂给游戏自身的账号层。
//!
//! 与闭源私服的差异：服务器地址不硬编码，全部由启动器从 OpenOrange.ini 读取后
//! 经共享内存投递；本 DLL 不做任何网络请求。

mod bridge;
mod capi;
mod inject;
mod insn;
mod log;
mod native;
mod script;
mod xlua;
mod patch_hook;

use std::ffi::c_void;
use windows::core::BOOL;
use windows::Win32::Foundation::HINSTANCE;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::{CreateThread, Sleep, THREAD_CREATION_FLAGS};


#[no_mangle]
pub extern "system" fn DllMain(
    hinst: HINSTANCE,
    reason: u32,
    _reserved: *mut c_void,
) -> BOOL {
    const DLL_PROCESS_ATTACH: u32 = 1;
    if reason == DLL_PROCESS_ATTACH {
        unsafe {
            let p = &raw mut log::MODULE;
            *p = hinst.0;
        }
        // 不要在这里做重活：只起一个线程。
        unsafe {
            let _ = CreateThread(None, 0, Some(bootstrap_thread), None, THREAD_CREATION_FLAGS(0), None);
        }
    }
    BOOL(1)
}

extern "system" fn bootstrap_thread(_param: *mut c_void) -> u32 {
    log::init();
    log::write("bootstrap thread started");

    // 等待 xlua.dll 被 Unity/xLua 加载。
    // 实测：注入后 30 秒内 GameAssembly.dll(78MB IL2CPP) 已加载但 xlua.dll 尚未出现，
    // 冷启动时 Unity 初始化 + 首场景加载可能远超 30 秒，因此给足 5 分钟并周期报告进度。
    // ★ 队列任务：官方 URL 依赖兜底（方案 A）。
    //   开关：环境变量 OO_PATCH_BYPASS=1 时启用。默认关闭，不改变既有行为。
    //   内部自会等待 GameAssembly.dll 解密就绪（序言校验通过才装 hook）。
    if std::env::var("OO_PATCH_BYPASS").as_deref() == Ok("1") {
        log::write("patch_hook: OO_PATCH_BYPASS=1, arming HybridLoad bypass");
        patch_hook::spawn_wait_thread();
    }

    if !wait_for_xlua(300_000) {
        log::write("FATAL: xlua.dll not loaded within 300s, giving up");
        return 1;
    }
    log::write("xlua.dll detected");

    // 选择挂钩点。
    //
    // luaL_loadbuffer 的真实函数体在 +10 处有 `lea rdx,[rip-0xBB0]`（RIP 相对寻址），
    // 复制到 trampoline 后基准改变 -> 实测宿主进程首次调用即崩溃，
    // 因此它**不可**用于复制式内联挂钩。
    //
    // 改用 luaL_loadstring：真实函数体序言是 `mov r8,-1` + 一条 8 字节 NOP，
    // 无 RIP 相对、无相对跳转，覆盖 15 字节完全安全。
    // 语义上 luaL_loadstring(L,s) 等价于 luaL_loadbuffer(L,s,strlen(s),s)，
    // 同样是「Lua 加载脚本」的入口。
    //
    // 若 luaL_loadstring 不可挂，则依次回退到 lua_load -> lua_gettop
    // （后者只用于捕获 lua_State，不加载脚本）。
    // 顺序有讲究：
    //   lua_load 是 Lua 标准实现里所有加载路径的共同汇聚点
    //     （luaL_loadbuffer / luaL_loadstring / luaL_loadfile 内部都调用它），
    //     因此覆盖最广。实测其真实体 RVA 0x46E00，无 RIP 相对寻址，可安全挂钩。
    //   luaL_loadstring / luaL_loadfile 作为回退。
    //   lua_gettop 只用于捕获 lua_State，不加载脚本。
    const CANDIDATES: &[&str] = &["lua_load", "luaL_loadstring", "luaL_loadfile", "lua_gettop"];

    let mut chosen: Option<(*mut std::ffi::c_void, String)> = None;
    for name in CANDIDATES {
        let Some(entry) = xlua::find_export(name) else {
            log::write(&format!("candidate {name}: not exported"));
            continue;
        };
        let (body, hops, notes) = unsafe { xlua::resolve_real(entry) };
        for n in &notes {
            log::write(&format!("candidate {name}: thunk {n}"));
        }
        unsafe {
            log::write(&format!(
                "candidate {name}: entry={entry:p} body={body:p} hops={hops} bytes={}",
                xlua::hex_dump(body as *const u8, 16)
            ));
        }
        match xlua::precheck_len(body as *const u8) {
            Ok(cover) => {
                log::write(&format!(
                    "candidate {name}: HOOKABLE, cover={cover} bytes"
                ));
                chosen = Some((body, name.to_string()));
                break;
            }
            Err(e) => log::write(&format!("candidate {name}: rejected -> {e}")),
        }
    }

    let Some((body, name)) = chosen else {
        log::write("FATAL: no hookable Lua entry point found in xlua.dll");
        return 3;
    };
    xlua::set_hooked_name(&name);
    log::write(&format!("hooking {name} at body {body:p}"));

    match xlua::install(body, hook_detour as *const c_void) {
        Ok(t) => log::write(&format!("hook OK, trampoline={t:p}")),
        Err(e) => {
            log::write(&format!("FATAL: hook refused on body {body:p}: {e}"));
            return 3;
        }
    }
    // 挂钩就绪后，等待 Lua 侧真正开始工作。
    log::write("waiting for xlua to load lua chunks...");
    let mut waited = 0u32;
    while waited < 600_000 {
        if INJECTED.load(std::sync::atomic::Ordering::SeqCst) {
            log::write("bootstrap script injected, detour going transparent");
            return 0;
        }
        unsafe { Sleep(50) };
        waited += 50;
    }
    log::write("WARN: no lua chunk seen in 10 minutes");
    0
}

/// 探测一组关键模块是否已加载，用于等待期间诊断游戏进展。
///
/// 不枚举全部模块（需要 ToolHelp/PSAPI 特性），只查我们关心的几个名字 ——
/// 这已足够判断 Unity/IL2CPP/Lua 三层各自走到哪一步。
fn probe_key_modules() -> Vec<(&'static str, bool)> {
    const NAMES: &[&str] = &[
        "UnityPlayer.dll",
        "GameAssembly.dll",
        "xlua.dll",
        "mono-2.0-bdwgc.dll",
        "FPX.dll",
        "NEP2.dll",
    ];
    NAMES
        .iter()
        .map(|n| {
            let w: Vec<u16> = n.encode_utf16().chain(std::iter::once(0)).collect();
            let ok = unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())) }.is_ok();
            (*n, ok)
        })
        .collect()
}

/// 等待 xlua.dll 被游戏加载。
fn wait_for_xlua(timeout_ms: u32) -> bool {
    let mut waited = 0u32;
    let w: Vec<u16> = "xlua.dll".encode_utf16().chain(std::iter::once(0)).collect();
    let mut last_report = 0u32;
    while waited < timeout_ms {
        if unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())) }.is_ok() {
            log::write(&format!("xlua.dll appeared after {} ms", waited));
            return true;
        }
        unsafe { Sleep(50) };
        waited += 50;

        // 每 10 秒报告一次进度与已加载模块，便于判断游戏是否在正常推进
        if waited - last_report >= 10_000 {
            last_report = waited;
            let probes = probe_key_modules();
            let ready: Vec<&str> = probes
                .iter()
                .filter(|(_, ok)| *ok)
                .map(|(n, _)| *n)
                .collect();
            log::write(&format!(
                "waiting for xlua.dll... {} ms elapsed; loaded: {:?}",
                waited, ready
            ));
        }
    }
    let probes = probe_key_modules();
    log::write(&format!(
        "xlua.dll never appeared in {timeout_ms} ms; module states = {:?}",
        probes
    ));
    false
}

pub static INJECTED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

// ---------------- luaL_loadbuffer detour ----------------

/// luaL_loadbuffer 的 x64 调用约定（SysV 之外的 Win64 ABI）：
///   RCX = L, RDX = buff, R8 = size, R9 = name
pub type LuaLoadBuffer =
    unsafe extern "C" fn(*mut c_void, *const u8, usize, *const u8) -> i32;

/// 我们的 detour。首次被调用时先注入 OO_Internal_* 与引导脚本，
/// 之后原样转发到 trampoline。
unsafe extern "C" fn hook_detour(
    l: *mut c_void,
    buff: *const u8,
    size: usize,
    name: *const u8,
) -> i32 {
    if !INJECTED.load(std::sync::atomic::Ordering::SeqCst) {
        // 只注入一次。失败也置位，避免每次加载 Lua 块都重试拖慢游戏。
        match inject::run(l) {
            Ok(()) => INJECTED.store(true, std::sync::atomic::Ordering::SeqCst),
            Err(e) => {
                log::write(&format!("bootstrap injection failed: {e}"));
                INJECTED.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        }
    }

    let tramp = xlua::trampoline();
    if tramp.is_null() {
        log::write("FATAL: trampoline missing, cannot forward call");
        return 1;
    }
    let real: LuaLoadBuffer = std::mem::transmute(tramp);
    real(l, buff, size, name)
}
