//! xlua.dll 的 Lua C API 动态绑定。
//!
//! 依据：对 xlua.dll 导出表的实际解析结果，共 277 个导出，
//! 其中包含完整的 lua_* / luaL_* C API（lua_pushcclosure / lua_setglobal /
//! lua_tolstring / lua_pcall / lua_settop / lua_gettop 等）。
//! 因此原生函数可以直接用标准 C API 注入，无需拼字符串脚本。

use std::ffi::c_void;
use windows::core::PCSTR;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};

pub type LuaState = *mut c_void;

/// lua_CFunction
pub type LuaCFunction = unsafe extern "C" fn(LuaState) -> i32;

pub fn xlua_handle() -> Option<windows::Win32::Foundation::HMODULE> {
    let w: Vec<u16> = "xlua.dll".encode_utf16().chain(std::iter::once(0)).collect();
    unsafe { GetModuleHandleW(windows::core::PCWSTR(w.as_ptr())).ok() }
}

/// 解析一个导出符号为函数指针。
pub unsafe fn resolve<T>(name: &str) -> Option<T> {
    let h = xlua_handle()?;
    let c = std::ffi::CString::new(name).ok()?;
    let p = GetProcAddress(h, PCSTR(c.as_ptr() as *const u8))?;
    Some(std::mem::transmute_copy(&p))
}

// 类型别名，便于阅读
type FnGetTop = unsafe extern "C" fn(LuaState) -> i32;
type FnSetTop = unsafe extern "C" fn(LuaState, i32);
type FnPushCClosure = unsafe extern "C" fn(LuaState, LuaCFunction, i32);
type FnSetGlobal = unsafe extern "C" fn(LuaState, *const i8);
type FnToLString = unsafe extern "C" fn(LuaState, i32, *mut usize) -> *const i8;
type FnToCStringLen = unsafe extern "C" fn(LuaState, i32, *mut usize) -> *const i8;
type FnPushString = unsafe extern "C" fn(LuaState, *const i8);
type FnLLoadBuffer = unsafe extern "C" fn(LuaState, *const u8, usize, *const i8) -> i32;
type FnPcall = unsafe extern "C" fn(LuaState, i32, i32, i32) -> i32;
type FnType = unsafe extern "C" fn(LuaState, i32) -> i32;

/// Lua 栈索引常量。
pub const LUA_GLOBALSINDEX: i32 = -10002;
pub const LUA_REGISTRYINDEX: i32 = -10000;
pub const LUA_TSTRING: i32 = 4;
pub const LUA_TFUNCTION: i32 = 6;
pub const LUA_MULTRET: i32 = -1;

/// 已解析的 API 集合。首次使用时解析一次，失败则返回 None 并留下原因。
pub struct Api {
    pub gettop: FnGetTop,
    pub settop: FnSetTop,
    pub pushcclosure: FnPushCClosure,
    pub setglobal: FnSetGlobal,
    pub tolstring: FnToLString,
    pub pushlstring: FnToCStringLen,
    pub pushstring: FnPushString,
    pub type_of: FnType,
}

// 注意：lua_tostring 在 Lua 中是宏 lua_tolstring(L, i, NULL)，不是导出符号。
// 实测 xlua.dll 的 277 个导出里只有 lua_tolstring，因此所有取字符串的地方
// 一律走 lua_tolstring（传 len 指针，不需要时传 null）。

impl Api {
    pub unsafe fn load() -> Result<Api, String> {
        macro_rules! need {
            ($n:literal, $t:ty) => {
                resolve::<$t>($n).ok_or_else(|| format!("xlua.dll 缺少导出: {}", $n))?
            };
        }
        Ok(Api {
            gettop: need!("lua_gettop", FnGetTop),
            settop: need!("lua_settop", FnSetTop),
            pushcclosure: need!("lua_pushcclosure", FnPushCClosure),
            setglobal: need!("lua_setglobal", FnSetGlobal),
            tolstring: need!("lua_tolstring", FnToLString),
            pushlstring: need!("lua_pushlstring", FnToCStringLen),
            pushstring: need!("lua_pushstring", FnPushString),
            type_of: need!("lua_type", FnType),
        })
    }
}

/// 读取栈顶（或指定索引）的字符串。
pub unsafe fn to_string(api: &Api, l: LuaState, idx: i32) -> String {
    let mut len = 0usize;
    let p = (api.tolstring)(l, idx, &mut len);
    if p.is_null() || len == 0 {
        return String::new();
    }
    let s = std::slice::from_raw_parts(p as *const u8, len);
    String::from_utf8_lossy(s).to_string()
}

/// 运行一段 Lua 代码，出错时把错误信息取回。
pub unsafe fn do_string(api: &Api, l: LuaState, code: &str) -> Result<(), String> {
    let load: FnLLoadBuffer =
        resolve("luaL_loadbuffer").ok_or("xlua.dll 缺少导出: luaL_loadbuffer")?;
    let pcall: FnPcall = resolve("lua_pcall").ok_or("xlua.dll 缺少导出: lua_pcall")?;

    let name = std::ffi::CString::new("@oo_internal").unwrap();
    let rc = load(l, code.as_ptr(), code.len(), name.as_ptr());
    if rc != 0 {
        let msg = to_string(api, l, -1);
        (api.settop)(l, -2);
        return Err(format!("luaL_loadbuffer rc={rc}: {msg}"));
    }
    let rc = pcall(l, 0, 0, 0);
    if rc != 0 {
        let msg = to_string(api, l, -1);
        (api.settop)(l, -2);
        return Err(format!("lua_pcall rc={rc}: {msg}"));
    }
    Ok(())
}

/// 载入一段 Lua chunk（不执行，只压栈，供调用方决定何时 pcall）。
///
/// ── 为什么用 luaL_loadbuffer 而不是被挂钩的 luaL_loadstring ──────────────
/// 我们挂钩的是 luaL_loadstring（它的真实函数体序言干净；luaL_loadbuffer 的真实体
/// 含 RIP 相对寻址 lea rdx,[rip-0xBB0]，无法复制进 trampoline）。
/// 因此 luaL_loadbuffer **完全未被修改**，直接调用它既安全、也不会重入。
///
/// ★ 历史教训：曾错误地把 trampoline 当作 luaL_loadbuffer 调用，但当时 trampoline
///   对应的是 luaL_loadstring（签名 (L, s)，2 个参数）。用 4 个参数去调它，
///   第 2 个参数被当成字符串指针，而它并非 NUL 结尾 -> Lua 读到垃圾 -> rc=3。
///   结论：trampoline 的签名必须与**被挂钩的那个函数**一致，不可混用。
pub unsafe fn load_chunk(
    l: LuaState,
    src: &str,
    chunk_name: &str,
) -> Result<(), String> {
    let load: FnLLoadBuffer =
        resolve("luaL_loadbuffer").ok_or("xlua.dll 缺少导出: luaL_loadbuffer")?;
    let name = std::ffi::CString::new(chunk_name).unwrap();
    let rc = load(l, src.as_ptr(), src.len(), name.as_ptr());
    if rc != 0 {
        return Err(format!("luaL_loadbuffer rc={rc}"));
    }
    Ok(())
}

/// 经由 trampoline 调用**被挂钩的那个函数**。签名必须与之匹配。
/// 当前挂钩 luaL_loadstring，因此这里是 (L, s) -> int。
pub unsafe fn load_string_via_trampoline(l: LuaState, src: &str) -> Result<(), String> {
    let tramp = crate::xlua::trampoline();
    if tramp.is_null() {
        return Err("trampoline not installed".into());
    }
    let f: unsafe extern "C" fn(LuaState, *const i8) -> i32 = std::mem::transmute(tramp);
    let s = std::ffi::CString::new(src).map_err(|e| format!("nul in source: {e}"))?;
    let rc = f(l, s.as_ptr());
    if rc != 0 {
        return Err(format!("luaL_loadstring(trampoline) rc={rc}"));
    }
    Ok(())
}

/// 直接调用被挂钩的 luaL_loadbuffer（会触发 detour）。仅测试用。
pub unsafe fn load_chunk_hooked(
    l: LuaState,
    src: &str,
    chunk_name: &str,
) -> Result<(), String> {
    let load: FnLLoadBuffer =
        resolve("luaL_loadbuffer").ok_or("xlua.dll 缺少导出: luaL_loadbuffer")?;
    let name = std::ffi::CString::new(chunk_name).unwrap();
    let rc = load(l, src.as_ptr(), src.len(), name.as_ptr());
    if rc != 0 {
        return Err(format!("luaL_loadbuffer rc={rc}"));
    }
    Ok(())
}

pub unsafe fn pcall(api: &Api, l: LuaState, nargs: i32, nresults: i32) -> Result<(), String> {
    let f: FnPcall = resolve("lua_pcall").ok_or("xlua.dll 缺少导出: lua_pcall")?;
    let rc = f(l, nargs, nresults, 0);
    if rc != 0 {
        let msg = to_string(api, l, -1);
        (api.settop)(l, -2);
        return Err(format!("lua_pcall rc={rc}: {msg}"));
    }
    Ok(())
}
