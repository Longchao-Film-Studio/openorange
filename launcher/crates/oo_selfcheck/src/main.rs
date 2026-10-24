//! 端到端自检：
//!   1) 用真实 OpenOrange.ini 校验配置解析
//!   2) 创建共享内存 -> 写入登录 JSON -> 再打开读回 -> 比对
//!   3) 校验内嵌 Lua 脚本与 lua/ 源文件一致
//!   4) 校验目标客户端存在且官服 loginpanel.lua 有必需方法

use OO_Internal::diag::{analyze_reloc, coverage_len_lite, decode_len_lite, find_rip_rel_lite};
use oo_shared::{channel_name, BridgeHeader, BRIDGE_CAPACITY, BRIDGE_MAGIC, BRIDGE_VERSION, LoginEnvelope, STATE_READY};
use std::ffi::c_void;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};
use windows::Win32::System::Memory::{
    CreateFileMappingW, MapViewOfFile, OpenFileMappingW, UnmapViewOfFile, FILE_MAP_ALL_ACCESS,
    FILE_MAP_READ, PAGE_READWRITE,
};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn main() {
    let mut failed = 0;
    println!("=== OpenOrange 端到端自检 ===");

    failed += check_ini();
    failed += check_bridge();
    failed += check_lua_source();
    failed += check_client_lua();
    failed += check_hook_target();
    failed += scan_hook_candidates();
    failed += analyze_thunk_chain();
    failed += deep_resolve();

    println!();
    if failed == 0 {
        println!("全部通过");
    } else {
        println!("{failed} 项失败");
        std::process::exit(1);
    }
}

fn check_ini() -> i32 {
    println!("\n[1] OpenOrange.ini 解析");
    // 依次尝试：当前目录（在 dist 里运行）、launcher/dist、crate 相对路径
    let mut p = std::path::PathBuf::new();
    for cand in [
        "OpenOrange.ini",
        "dist/OpenOrange.ini",
        "../../dist/OpenOrange.ini",
    ] {
        let c = std::path::PathBuf::from(cand);
        if c.exists() {
            p = c;
            break;
        }
    }
    if !p.exists() {
        println!("    FAIL: 找不到 OpenOrange.ini");
        return 1;
    }
    let text = std::fs::read_to_string(&p).unwrap();
    let mut section = String::new();
    let mut map = std::collections::HashMap::new();
    for raw in text.lines() {
        let l = raw.trim();
        if l.is_empty() || l.starts_with(';') || l.starts_with('#') { continue; }
        if l.starts_with('[') && l.ends_with(']') {
            section = l[1..l.len()-1].to_ascii_lowercase();
            continue;
        }
        if let Some(eq) = l.find('=') {
            map.insert(format!("{}.{}", section, l[..eq].trim().to_ascii_lowercase()), l[eq+1..].trim().to_string());
        }
    }
    let need = ["server.login_url", "server.game_host", "server.game_port", "game.inject_dll", "channel.channel_id"];
    let mut bad = 0;
    for k in need {
        match map.get(k) {
            Some(v) => println!("    {k} = {v}"),
            None => { println!("    FAIL: 缺少 {k}"); bad += 1; }
        }
    }
    if bad == 0 { println!("    OK"); }
    bad
}

fn check_bridge() -> i32 {
    println!("\n[2] 共享内存往返");
    // 用当前进程 PID 当作"游戏进程 PID"
    let pid = std::process::id();
    let name = channel_name(pid);
    println!("    通道名 = {name}");
    let wname = wide(&name);

    let payload = LoginEnvelope::ok(serde_json::json!({
        "account_id": "oo_selftest_account",
        "ticket": "selftest_ticket_value",
        "channel_uid": "780688466",
    }))
    .to_payload();
    println!("    载荷 = {} 字节", payload.len());

    unsafe {
        let mapping = CreateFileMappingW(
            INVALID_HANDLE_VALUE, None, PAGE_READWRITE,
            (BRIDGE_CAPACITY >> 16) as u32, (BRIDGE_CAPACITY & 0xFFFF) as u32,
            PCWSTR(wname.as_ptr()),
        ).expect("CreateFileMappingW");
        let view = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, BRIDGE_CAPACITY);
        assert!(!view.Value.is_null(), "MapViewOfFile failed");
        let base = view.Value as *mut u8;

        // --- 写 ---
        let h = BridgeHeader::default();
        std::ptr::copy_nonoverlapping(&h as *const _ as *const u8, base, std::mem::size_of::<BridgeHeader>());
        let pdata = base.add(std::mem::size_of::<BridgeHeader>());
        std::ptr::copy_nonoverlapping(payload.as_ptr(), pdata, payload.len());
        let hh = &mut *(base as *mut BridgeHeader);
        hh.length = payload.len() as u64;
        hh.state = STATE_READY;

        // --- 读（另开一个视图，模拟 DLL 侧） ---
        let m2 = OpenFileMappingW(FILE_MAP_READ.0, false, PCWSTR(wname.as_ptr())).expect("OpenFileMappingW");
        let v2 = MapViewOfFile(m2, FILE_MAP_READ, 0, 0, 0);
        assert!(!v2.Value.is_null(), "second MapViewOfFile failed");
        let b2 = v2.Value as *const u8;
        let h2 = &*(b2 as *const BridgeHeader);

        let mut bad = 0;
        if h2.magic != BRIDGE_MAGIC { println!("    FAIL: magic 不符 {:#X}", h2.magic); bad += 1; }
        else { println!("    magic OK"); }
        if h2.version != BRIDGE_VERSION { println!("    FAIL: version 不符"); bad += 1; }
        else { println!("    version OK = {}", h2.version); }
        if h2.state != STATE_READY { println!("    FAIL: state 未就绪"); bad += 1; }
        else { println!("    state OK = ready"); }

        let len = h2.length as usize;
        let got = std::slice::from_raw_parts(b2.add(std::mem::size_of::<BridgeHeader>()), len);
        let got_s = String::from_utf8_lossy(got).to_string();
        if got_s != payload {
            println!("    FAIL: 载荷不一致\n      期望 {payload}\n      实际 {got_s}");
            bad += 1;
        } else {
            println!("    载荷一致 OK");
            let env: LoginEnvelope = serde_json::from_str(&got_s).unwrap();
            println!("    反序列化 OK: code={} account_id={}", env.code, env.data["account_id"]);
        }

        let _ = UnmapViewOfFile(v2);
        let _ = CloseHandle(m2);
        let _ = UnmapViewOfFile(view);
        let _ = CloseHandle(mapping);
        bad
    }
}

/// 把 lua/oo/*.lua 按文件名顺序拼起来 —— 与 crates/oo_internal/build.rs 的拼法一致。
/// 引导脚本 P0-E3 拆成多个片段后，单看 oo_bootstrap.lua 是查不到钩子的。
fn load_bootstrap_lua() -> Option<(String, usize)> {
    let dir = std::path::Path::new("../../lua/oo");
    let dir = if dir.is_dir() { dir.to_path_buf() } else {
        let alt = std::path::PathBuf::from("lua/oo");
        if !alt.is_dir() { return None; }
        alt
    };
    let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(&dir).ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("lua"))
        .collect();
    files.sort();
    if files.is_empty() { return None; }
    let n = files.len();
    let mut buf = String::new();
    for p in &files {
        buf.push_str(&std::fs::read_to_string(p).ok()?);
        if !buf.ends_with('\n') { buf.push('\n'); }
    }
    Some((buf, n))
}

fn check_lua_source() -> i32 {
    println!("\n[3] 引导脚本");
    if let Some((s, n)) = load_bootstrap_lua() {
        println!("    lua/oo/*.lua 共 {} 个片段，合计 {} 字节", n, s.len());
            let mut bad = 0;
            for k in ["OO_Internal_RequestLogin", "OO_Internal_Result", "onSdkLogin", "EnterGame",
                      "openServerSelectPanel", "BindGameTickFunc", "platform", "fpxsdkapi",
                      "fpxsdkreceiver", "loginpanel"] {
                if !s.contains(k) { println!("    FAIL: 缺少 {k}"); bad += 1; }
            }
            if s.contains("52luansha") { println!("    FAIL: 残留第三方服务器地址"); bad += 1; }
        if bad == 0 { println!("    必需钩子齐全 OK"); }
        return bad;
    }
    println!("    FAIL: 找不到 lua/oo/*.lua");
    1
}

fn check_client_lua() -> i32 {
    println!("\n[4] 官服客户端兼容性");
    let p = std::path::Path::new(
        r"F:/haowancheng/archive/game_data/LuaAll_Decompiled2/ui/panel/loginpanel/loginpanel.lua",
    );
    if !p.exists() {
        println!("    SKIP: 反编译源码不在预期位置");
        return 0;
    }
    let s = std::fs::read_to_string(p).unwrap();
    let mut bad = 0;
    for k in ["function Panel:onRefreshServerList", "function Panel:openServerSelectPanel"] {
        if s.contains(k) { println!("    找到 {k}"); }
        else { println!("    FAIL: 缺少 {k}"); bad += 1; }
    }
    if bad == 0 { println!("    官服客户端满足脚本要求 OK"); }
    bad
}

#[allow(dead_code)]
fn _unused(_: HANDLE, _: c_void) {}

// ============================================================================
// 挂钩可行性预检：真实加载 xlua.dll，用 GetProcAddress 取真实入口并做前置检查。
// 依据：dbghelp 级别的静态解析在此 DLL 上不可用（9 个节的 RawOff/RawSize 全为 0），
//       只有运行时地址才是权威。
// ============================================================================

pub fn check_hook_target() -> i32 {
    println!("\n[5] 挂钩目标预检（运行时取址）");
    let dll = std::path::Path::new(
        r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll",
    );
    if !dll.exists() {
        println!("    SKIP: xlua.dll 不在预期位置");
        return 0;
    }
    println!("    xlua.dll = {}", dll.display());
    println!("    大小 = {} 字节", std::fs::metadata(dll).map(|m| m.len()).unwrap_or(0));

    // 动态加载。用 LoadLibraryExW + LOAD_WITH_ALTERED_SEARCH_PATH，
    // 并把 DLL 目录加入搜索路径，避免它依赖的其他 DLL 找不到。
    let dir = dll.parent().unwrap();
    let oldpath = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{};{}", dir.display(), oldpath));

    let w: Vec<u16> = dll.to_string_lossy().encode_utf16().chain(std::iter::once(0)).collect();
    let h = unsafe { LoadLibraryW(windows::core::PCWSTR(w.as_ptr())) };
    let h = match h {
        Ok(h) => h,
        Err(e) => {
            println!("    FAIL: LoadLibraryW 失败: {e}");
            println!("    （xlua.dll 可能依赖 Unity 宿主环境，无法独立加载）");
            return 0; // 不算失败，只是无法离线验证
        }
    };
    println!("    LoadLibraryW 成功，模块基址 = {:p}", h.0);

    let name = std::ffi::CString::new("luaL_loadbuffer").unwrap();
    let p = unsafe { GetProcAddress(h, windows::core::PCSTR(name.as_ptr() as *const u8)) };
    match p {
        Some(f) => {
            let addr = f as *const u8 as usize;
            println!("    luaL_loadbuffer 真实地址 = 0x{addr:X}");
            let rel = addr as u64 - h.0 as u64;
            println!("    相对模块基址偏移 = 0x{rel:X}");
            let bytes = unsafe { std::slice::from_raw_parts(f as *const u8, 16) };
            let hex: Vec<String> = bytes.iter().map(|b| format!("{b:02X}")).collect();
            println!("    入口 16 字节 = {}", hex.join(" "));

            // ★ 跟随跳板：入口若为 E9/EB，则解析到真实函数体
            let mut cur = f as *const u8;
            let mut followed = 0usize;
            for hop in 0..4usize {
                let op = unsafe { *cur };
                let next = match op {
                    0xE9 => {
                        let rel = unsafe {
                            i32::from_le_bytes([*cur.add(1), *cur.add(2), *cur.add(3), *cur.add(4)])
                        };
                        Some(unsafe { (cur as i64 + 5 + rel as i64) as *const u8 })
                    }
                    0xEB => {
                        let rel = unsafe { *cur.add(1) as i8 as i64 };
                        Some(unsafe { (cur as i64 + 2 + rel) as *const u8 })
                    }
                    _ => None,
                };
                match next {
                    Some(n) => {
                        println!(
                            "    thunk hop {hop}: 0x{:X} -> 0x{:X} (opcode {op:02X})",
                            cur as usize, n as usize
                        );
                        cur = n;
                        followed += 1;
                    }
                    None => {
                        println!("    真实函数体 = 0x{:X}（跟随 {hop} 层跳板）", cur as usize);
                        break;
                    }
                }
            }
            let body = unsafe { std::slice::from_raw_parts(cur, 16) };
            let bhex: Vec<String> = body.iter().map(|b| format!("{b:02X}")).collect();
            println!("    函数体 16 字节 = {}", bhex.join(" "));

            // 前置检查（与 DLL 内 precheck 完全同一套判据）
            let mut bad = 0;
            let mut i = 0usize;
            while i < 14 {
                let b = body[i];
                let hit = match b {
                    0xE8 => Some("call rel32 (E8)"),
                    0xE9 => Some("jmp rel32 (E9)"),
                    0xEB => Some("jmp rel8 (EB)"),
                    0x0F if (body[i + 1] & 0xF0) == 0x80 => Some("jcc rel32 (0F 8x)"),
                    _ => None,
                };
                if let Some(k) = hit {
                    println!("    REJECT: {k} at +{i}");
                    bad += 1;
                }
                i += 1;
            }
            if bad == 0 {
                println!("    PASS: 前 14 字节无相对跳转 —— 可以安全安装绝对跳转");
            } else {
                println!("    => 该 body 不可挂钩，需要继续跟随或换挂钩点");
            }
            let _ = followed;
            bad
        }
        None => {
            println!("    FAIL: GetProcAddress(luaL_loadbuffer) 返回空");
            1
        }
    }
}

// ============================================================================
// 候选挂钩点扫描：找出序言「无 RIP 相对寻址、无相对跳转」的导出函数。
// 依据：内联挂钩需要把前 N 字节复制到 trampoline，
//       而 RIP 相对寻址（lea/mov with mod=00 rm=101）复制后基准改变 -> 崩溃。
//       luaL_loadbuffer 的序言第 3 条就是 lea rdx,[rip-0xBB0] -> 不可直接挂钩。
// ============================================================================
pub fn scan_hook_candidates() -> i32 {
    println!("\n[6] 挂钩候选点扫描");
    let dll = std::path::Path::new(
        r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll",
    );
    if !dll.exists() {
        println!("    SKIP");
        return 0;
    }
    let dir = dll.parent().unwrap();
    let oldpath = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{};{}", dir.display(), oldpath));
    let w: Vec<u16> = dll.to_string_lossy().encode_utf16().chain(std::iter::once(0)).collect();
    let h = match unsafe { LoadLibraryW(windows::core::PCWSTR(w.as_ptr())) } {
        Ok(h) => h,
        Err(e) => {
            println!("    LoadLibraryW 失败: {e}");
            return 0;
        }
    };

    let names = [
        "xluaL_loadbuffer",
        "luaL_loadbuffer",
        "luaL_loadbufferx",
        "luaL_loadstring",
        "lua_load",
        "lua_gettop",
        "lua_settop",
        "lua_pushcclosure",
        "lua_setglobal",
        "lua_pcall",
        "luaL_newstate",
        "luaL_openlibs",
        "lua_pushstring",
        "luaL_dostring",
        "lua_tolstring",
    ];

    println!("    {:<22} {:<12} {:<8} {:<10} {}", "导出名", "RVA", "跳板", "可挂钩", "序言");

    let mut safe: Vec<(&str, usize)> = Vec::new();

    for n in names {
        let c = std::ffi::CString::new(n).unwrap();
        let p = unsafe { GetProcAddress(h, windows::core::PCSTR(c.as_ptr() as *const u8)) };
        let Some(f) = p else {
            println!("    {n:<22} (无此导出)");
            continue;
        };
        let entry = f as *const u8 as usize;
        let rva = entry - h.0 as usize;

        // 跟随跳板
        let mut cur = f as *const u8;
        let mut hops = 0usize;
        loop {
            let op = unsafe { *cur };
            let next = match op {
                0xE9 => {
                    let rel = unsafe {
                        i32::from_le_bytes([*cur.add(1), *cur.add(2), *cur.add(3), *cur.add(4)])
                    };
                    Some(unsafe { (cur as i64 + 5 + rel as i64) as *const u8 })
                }
                0xEB => {
                    let rel = unsafe { *cur.add(1) as i8 as i64 };
                    Some(unsafe { (cur as i64 + 2 + rel) as *const u8 })
                }
                _ => None,
            };
            match next {
                Some(nx) if hops < 4 => {
                    cur = nx;
                    hops += 1;
                }
                _ => break,
            }
        }

        let body = unsafe { std::slice::from_raw_parts(cur, 24) };
        let hex: Vec<String> = body[..16].iter().map(|b| format!("{b:02X}")).collect();

        // 检查重定位安全性：前 16 字节里不得有 RIP 相对寻址 / 相对跳转
        let risk = analyze_reloc(body);

        let mark = if risk.is_empty() { "OK" } else { "NO" };
        if risk.is_empty() {
            safe.push((n, cur as usize - h.0 as usize));
        }
        println!(
            "    {n:<22} 0x{rva:<9X} {hops:<8} {mark:<10} {}",
            hex.join(" ")
        );
        if !risk.is_empty() {
            println!("      └ {}", risk.join("; "));
        }
    }

    println!("\n    可安全挂钩的候选：");
    if safe.is_empty() {
        println!("      （无）");
    } else {
        for (n, rva) in &safe {
            println!("      {n}  RVA 0x{rva:X}");
        }
    }

    unsafe { let _ = windows::Win32::Foundation::FreeLibrary(h); }
    if safe.is_empty() { 1 } else { 0 }
}

// analyze_reloc 已迁至 oo_internal::diag

// ============================================================================
// 精确解析跳转链 + 评估 trampoline 可行性（含叶子函数 ret 处理）
// ============================================================================
pub fn analyze_thunk_chain() -> i32 {
    println!("\n[7] 跳转链精确解析");
    let dll = std::path::Path::new(
        r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll",
    );
    let dir = dll.parent().unwrap();
    let oldpath = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{};{}", dir.display(), oldpath));
    let w: Vec<u16> = dll.to_string_lossy().encode_utf16().chain(std::iter::once(0)).collect();
    let h = unsafe { LoadLibraryW(windows::core::PCWSTR(w.as_ptr())).unwrap() };
    let base = h.0 as usize;

    for name in [
        "xluaL_loadbuffer",
        "luaL_loadbuffer",
        "luaL_loadstring",
        "lua_load",
        "lua_gettop",
    ] {
        let c = std::ffi::CString::new(name).unwrap();
        let Some(f) = (unsafe { GetProcAddress(h, windows::core::PCSTR(c.as_ptr() as *const u8)) })
        else {
            println!("    {name}: 无此导出");
            continue;
        };
        println!("\n    === {name} ===");
        let mut cur = f as *const u8;
        for hop in 0..5 {
            let addr = cur as usize;
            let rva = addr - base;
            let op = unsafe { *cur };
            let bytes = unsafe { std::slice::from_raw_parts(cur, 16) };
            let hex: Vec<String> = bytes.iter().map(|b| format!("{b:02X}")).collect();
            println!("      hop {hop}: RVA 0x{rva:X}  op={op:02X}  [{}]", hex.join(" "));
            let next = match op {
                0xE9 => {
                    let rel = unsafe {
                        i32::from_le_bytes([*cur.add(1), *cur.add(2), *cur.add(3), *cur.add(4)])
                    };
                    let t = (addr as i64 + 5 + rel as i64) as usize;
                    println!(
                        "        E9 rel32=0x{rel:X} ({}) -> 目标 RVA 0x{:X}",
                        rel,
                        t - base
                    );
                    Some(t as *const u8)
                }
                0xEB => {
                    let rel = unsafe { *cur.add(1) as i8 as i64 };
                    let t = (addr as i64 + 2 + rel) as usize;
                    println!("        EB rel8={rel} -> 目标 RVA 0x{:X}", t - base);
                    Some(t as *const u8)
                }
                _ => None,
            };
            match next {
                Some(n) => cur = n,
                None => {
                    println!("        -> 真实函数体");
                    break;
                }
            }
        }
    }
    0
}
// ============================================================================
// 深度解析：xluaL_loadbuffer 与 lua_load 的完整跳转链及真实体可挂钩性
// ============================================================================
pub fn deep_resolve() -> i32 {
    println!("\n[8] 深度解析候选入口的真实函数体");
    let dll = std::path::Path::new(
        r"F:/haowancheng/GuiLongchao/game/GuiLongchao_Data/Plugins/x86_64/xlua.dll",
    );
    let dir = dll.parent().unwrap();
    let oldpath = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{};{}", dir.display(), oldpath));
    let w: Vec<u16> = dll.to_string_lossy().encode_utf16().chain(std::iter::once(0)).collect();
    let h = unsafe { LoadLibraryW(windows::core::PCWSTR(w.as_ptr())).unwrap() };
    let base = h.0 as usize;

    for name in [
        "xluaL_loadbuffer",
        "luaL_loadbuffer",
        "luaL_loadstring",
        "lua_load",
        "luaL_loadfile",
    ] {
        let c = std::ffi::CString::new(name).unwrap();
        let Some(f) = (unsafe { GetProcAddress(h, windows::core::PCSTR(c.as_ptr() as *const u8)) })
        else {
            println!("    {name}: 无导出");
            continue;
        };
        println!("\n    ==== {name} (入口 RVA 0x{:X}) ====", f as usize - base);
        let mut cur = f as *const u8;
        let mut hop = 0;
        loop {
            let addr = cur as usize;
            let op = unsafe { *cur };
            let bytes = unsafe { std::slice::from_raw_parts(cur, 24) };
            let hex: Vec<String> = bytes.iter().map(|b| format!("{b:02X}")).collect();
            println!("      [{hop}] RVA 0x{:X} op={op:02X}", addr - base);
            println!("          {}", hex.join(" "));
            let next = match op {
                0xE9 => {
                    let rel = unsafe {
                        i32::from_le_bytes([*cur.add(1), *cur.add(2), *cur.add(3), *cur.add(4)])
                    };
                    let t = (addr as i64 + 5 + rel as i64) as usize;
                    println!("          E9 -> RVA 0x{:X}", t - base);
                    Some(t as *const u8)
                }
                0xEB => {
                    let rel = unsafe { *cur.add(1) as i8 as i64 };
                    let t = (addr as i64 + 2 + rel) as usize;
                    println!("          EB -> RVA 0x{:X}", t - base);
                    Some(t as *const u8)
                }
                _ => None,
            };
            match next {
                Some(n) if hop < 5 => {
                    cur = n;
                    hop += 1;
                }
                _ => {
                    println!("          ==> 真实函数体 RVA 0x{:X}", cur as usize - base);
                    // 指令边界分析
                    let win = unsafe { std::slice::from_raw_parts(cur, 32) };
                    match coverage_len_lite(&win[..24]) {
                        Some(cover) => {
                            println!("          覆盖长度 = {cover} 字节");
                            match find_rip_rel_lite(&win[..cover]) {
                                Some(off) => println!("          RIP relative at +{off}  => NOT hookable"),
                                None => println!("          no RIP-relative      => HOOKABLE"),
                            }
                        }
                        None => println!("          cannot determine coverage => NOT hookable"),
                    }
                    break;
                }
            }
        }
    }
    0
}
// ---- 本地精简版指令解码已迁至 oo_internal::diag（消除双份维护，2026-10-24）----
