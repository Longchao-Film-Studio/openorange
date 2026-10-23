//! xlua.dll 动态绑定与 Lua 入口内联挂钩。
//!
//! ── 实测事实（构建整个方案的立足点，逐条都有运行时证据）────────────────
//!
//! 1) xlua.dll 的节表刻意抹掉了 raw 信息：.text/.rdata/.data/.pdata/.idata/
//!    .tls/.00cfg/.200 这 8 个节的 PointerToRawData 与 SizeOfRawData 全为 0，
//!    只有 .201 保留 RawOff=0x400。=> 静态 RVA->偏移 映射不可用，
//!    必须用运行时 GetProcAddress。
//!
//! 2) 所有 Lua C API 的导出入口都不是函数体，而是 5 字节 E9 跳板，
//!    按 5 字节步长连续排布（导出跳板表 / ILT）。实测：
//!      lua_gettop        入口 RVA 0x211C  -> 真实体 RVA 0x46BE0
//!      luaL_loadstring   入口 RVA 0x1BC3E -> 真实体 RVA 0x5CCD0
//!      lua_load          入口 RVA 0x21A3F -> 真实体 RVA 0x46E00
//!      luaL_loadbuffer   入口 RVA 0x7AB8  -> 真实体 RVA 0x5CA30
//!    验证：(0x1BC3E - 0x1B37E) / 5 = 448 整除，证明步长确为 5。
//!    => 绝不能在入口打补丁，那会覆盖掉后续多个导出的跳板。必须跟随到真实体。
//!
//! 3) 真实函数体的序言决定它能否被「复制前 N 字节」式挂钩：
//!      luaL_loadbuffer @0x5CA30: 48 83 EC 38 / 48 89 54 24 20 / 48 8D 15 ...
//!         第 3 条是 lea rdx,[rip-0xBB0] —— RIP 相对寻址。
//!         复制到 trampoline 后 rip 基准改变，算出的指针完全错误，
//!         实测后果：宿主进程在该函数第一次被调用时立即崩溃。
//!         改覆盖长度（14->16）无效，因为问题不是截断而是重定位。
//!      luaL_loadstring @0x5CCD0: 49 C7 C0 FF FF FF FF (mov r8,-1)
//!                               66 0F 1F 84 00 00 00 00 00 (nop, 8 字节)
//!         无 RIP 相对、无相对跳转，覆盖 15 字节安全。
//!      lua_gettop @0x46BE0: 48 8B 41 10 / 48 2B 41 18 / 48 C1 F8 04 / C3
//!         纯寄存器运算的叶子函数，无 RIP 相对。
//!
//! ── 挂钩流程 ───────────────────────────────────────────────────────────
//!     find_export(name) : GetProcAddress
//!     resolve_real()    : 若入口是 E9/EB 跳板则跟随到真实函数体
//!     precheck_len()    : 相对跳转检查 + 指令边界覆盖长度 + RIP 相对寻址检查
//!     install()         : 写 FF 25 绝对跳转，生成 trampoline

use crate::log;
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, Ordering};
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Memory::{
    VirtualAlloc, VirtualProtect, MEM_COMMIT, MEM_RESERVE, PAGE_EXECUTE_READWRITE,
    PAGE_PROTECTION_FLAGS,
};

/// 容纳 FF 25 + imm64 所需的最小覆盖长度。
pub const HOOK_LEN: usize = 14;

/// 本次挂钩实际使用的覆盖长度（按指令边界对齐，>= HOOK_LEN）。
static COVER_LEN: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(HOOK_LEN);

pub fn cover_len() -> usize {
    COVER_LEN.load(Ordering::SeqCst)
}

/// 跟随跳板的最大层数。
const MAX_THUNK_HOPS: usize = 4;

pub static HOOK_INSTALLED: AtomicBool = AtomicBool::new(false);

static TRAMPOLINE: std::sync::atomic::AtomicPtr<c_void> =
    std::sync::atomic::AtomicPtr::new(std::ptr::null_mut());

pub fn trampoline() -> *mut c_void {
    TRAMPOLINE.load(Ordering::SeqCst)
}

/// 记录实际挂钩的函数名，供日志与调试。
static HOOKED_NAME: std::sync::Mutex<String> = std::sync::Mutex::new(String::new());

pub fn hooked_name() -> String {
    HOOKED_NAME.lock().map(|g| g.clone()).unwrap_or_default()
}

pub fn set_hooked_name(n: &str) {
    if let Ok(mut g) = HOOKED_NAME.lock() {
        *g = n.to_string();
    }
}

/// 取 xlua.dll 模块句柄。
fn xlua_module() -> Option<windows::Win32::Foundation::HMODULE> {
    let w: Vec<u16> = "xlua.dll".encode_utf16().chain(std::iter::once(0)).collect();
    unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())).ok() }
}

/// 取任意导出符号的入口地址。
pub fn find_export(name: &str) -> Option<*mut c_void> {
    let h = xlua_module()?;
    let c = std::ffi::CString::new(name).ok()?;
    unsafe { GetProcAddress(h, windows::core::PCSTR(c.as_ptr() as *const u8)) }
        .map(|f| f as *mut c_void)
}

/// 取 luaL_loadbuffer 的导出入口（保留兼容）。
pub fn find_loadbuffer() -> Option<*mut c_void> {
    find_export("luaL_loadbuffer")
}

unsafe fn thunk_target(p: *const u8) -> Option<*const u8> {
    match *p {
        0xE9 => {
            let rel = i32::from_le_bytes([*p.add(1), *p.add(2), *p.add(3), *p.add(4)]);
            Some((p as i64 + 5 + rel as i64) as *const u8)
        }
        0xEB => {
            let rel = *p.add(1) as i8 as i64;
            Some((p as i64 + 2 + rel) as *const u8)
        }
        _ => None,
    }
}

/// 跟随跳板，返回 (真实函数体, 层数, 日志)。
pub unsafe fn resolve_real(entry: *mut c_void) -> (*mut c_void, usize, Vec<String>) {
    let mut cur = entry as *const u8;
    let mut notes = Vec::new();
    for hop in 0..MAX_THUNK_HOPS {
        match thunk_target(cur) {
            Some(next) => {
                notes.push(format!(
                    "hop {hop}: {:p} -> {:p} (opcode {:02X})",
                    cur, next, *cur
                ));
                if (next as usize) < 0x1000 {
                    notes.push(format!("invalid target {:p}, aborting chain", next));
                    return (cur as *mut c_void, hop + 1, notes);
                }
                cur = next;
            }
            None => {
                if hop == 0 {
                    notes.push(format!("{:p} is the real body (no thunk)", cur));
                } else {
                    notes.push(format!("real body reached at {:p} after {hop} hop(s)", cur));
                }
                return (cur as *mut c_void, hop, notes);
            }
        }
    }
    notes.push(format!("thunk chain exceeded {MAX_THUNK_HOPS} hops, using {:p}", cur));
    (cur as *mut c_void, MAX_THUNK_HOPS, notes)
}

pub unsafe fn hex_dump(p: *const u8, n: usize) -> String {
    let b = std::slice::from_raw_parts(p, n);
    b.iter().map(|x| format!("{x:02X}")).collect::<Vec<_>>().join(" ")
}

/// 挂钩前置检查 + 覆盖长度计算。
///
/// 检查项：
///   1) 前 14 字节内无相对跳转（E8/E9/EB/0F8x）
///   2) 覆盖长度按完整指令边界对齐
///   3) 无 RIP 相对寻址 —— 最关键的一条，见文件头说明第 3 点
pub fn precheck_len(target: *const u8) -> Result<usize, String> {
    if target.is_null() {
        return Err("target is null".into());
    }
    if (target as usize) < 0x1000 {
        return Err(format!("target {target:p} is not a plausible code address"));
    }

    let win = unsafe { std::slice::from_raw_parts(target, crate::insn::MAX_HOOK_LEN) };

    // 1) 相对跳转检查
    let bytes = &win[..HOOK_LEN];
    let mut i = 0usize;
    while i < HOOK_LEN {
        match bytes[i] {
            0xE8 => return Err(format!("call rel32 (E8) at +{i}: not relocatable")),
            0xE9 => return Err(format!("jmp rel32 (E9) at +{i}: not relocatable")),
            0xEB => return Err(format!("jmp rel8 (EB) at +{i}: not relocatable")),
            0x0F if i + 1 < HOOK_LEN && (bytes[i + 1] & 0xF0) == 0x80 => {
                return Err(format!("jcc rel32 (0F 8x) at +{i}: not relocatable"));
            }
            _ => i += 1,
        }
    }

    // 2) 覆盖长度
    let cover = crate::insn::coverage_len(win).ok_or_else(|| {
        format!(
            "cannot determine instruction-aligned coverage; first bytes = {}",
            unsafe { hex_dump(target, 16) }
        )
    })?;

    // 3) RIP 相对寻址检查
    if let Some(off) = crate::insn::find_rip_relative(&win[..cover]) {
        return Err(format!(
            "instruction at +{off} uses RIP-relative addressing; copying to trampoline changes the base and would crash. first bytes = {}",
            unsafe { hex_dump(target, 16) }
        ));
    }

    Ok(cover)
}

pub fn precheck(target: *const u8) -> Option<String> {
    precheck_len(target).err()
}

unsafe fn build_trampoline(body: *const u8, cover: usize) -> Result<*mut u8, String> {
    let mem = VirtualAlloc(None, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if mem.is_null() {
        return Err("VirtualAlloc(trampoline) failed".into());
    }
    let t = mem as *mut u8;
    std::ptr::copy_nonoverlapping(body, t, cover);

    let back = body.add(cover) as u64;
    let off = cover;
    *t.add(off) = 0xFF;
    *t.add(off + 1) = 0x25;
    *t.add(off + 2) = 0x00;
    *t.add(off + 3) = 0x00;
    *t.add(off + 4) = 0x00;
    *t.add(off + 5) = 0x00;
    std::ptr::write_unaligned(t.add(off + 6) as *mut u64, back);
    Ok(t)
}

/// 通用内联挂钩（不触碰 xlua 的单例状态）。
///
/// 一个进程里可能不止一个 hook 目标（xlua 的 luaL_loadstring + GameAssembly 的
/// HybridLoad 回调），所以真正的安装逻辑只允许有一条实现（R3）。
/// xlua 专用的单例（COVER_LEN/TRAMPOLINE/HOOK_INSTALLED）只由下面的 `install`
/// 薄封装维护，其他调用方一律用本函数并自行保存 trampoline。
pub fn install_generic(
    body: *mut c_void,
    detour: *const c_void,
    label: &str,
) -> Result<*mut c_void, String> {
    let cover = precheck_len(body as *const u8)?;
    log::write(&format!(
        "hook[{label}]: coverage = {cover} bytes (min {HOOK_LEN}), {} instruction(s)",
        unsafe { count_insns(body as *const u8, cover) }
    ));

    let tramp = unsafe { build_trampoline(body as *const u8, cover)? };

    unsafe {
        let mut old = PAGE_PROTECTION_FLAGS(0);
        VirtualProtect(body, cover, PAGE_EXECUTE_READWRITE, &mut old)
            .map_err(|e| format!("VirtualProtect(RWX) failed: {e}"))?;

        let t = body as *mut u8;
        *t = 0xFF;
        *t.add(1) = 0x25;
        *t.add(2) = 0x00;
        *t.add(3) = 0x00;
        *t.add(4) = 0x00;
        *t.add(5) = 0x00;
        std::ptr::write_unaligned(t.add(6) as *mut u64, detour as u64);

        let mut restored = PAGE_PROTECTION_FLAGS(0);
        let _ = VirtualProtect(body, cover, old, &mut restored);
    }

    log::write(&format!(
        "hook[{label}] installed: body={:p} detour={:p} trampoline={:p}",
        body, detour, tramp
    ));
    Ok(tramp as *mut c_void)
}

/// 在真实函数体上安装绝对跳转（xlua 专用：同时维护单例状态供 detour 转发）。
/// 返回 trampoline。
pub fn install(body: *mut c_void, detour: *const c_void) -> Result<*mut c_void, String> {
    let tramp = install_generic(body, detour, "xlua")?;
    COVER_LEN.store(cover_len_for(body)?, Ordering::SeqCst);
    TRAMPOLINE.store(tramp as *mut c_void, Ordering::SeqCst);
    HOOK_INSTALLED.store(true, Ordering::SeqCst);
    Ok(tramp as *mut c_void)
}

/// 重算覆盖长度（install 薄封装用）。
fn cover_len_for(body: *mut c_void) -> Result<usize, String> {
    precheck_len(body as *const u8)
}

unsafe fn count_insns(p: *const u8, cover: usize) -> usize {
    let win = std::slice::from_raw_parts(p, cover + 16);
    let mut off = 0usize;
    let mut n = 0usize;
    while off < cover {
        match crate::insn::decode_len(&win[off..]) {
            Some(l) if l > 0 => {
                off += l;
                n += 1;
            }
            _ => break,
        }
    }
    n
}
