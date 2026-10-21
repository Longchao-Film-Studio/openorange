//! 测试宿主：模拟游戏进程的关键特征，用来验证 OO_Internal.dll 的注入链路。
//!
//! 它做的事情：
//!   1. 加载真实的 xlua.dll（这样 DLL 能找到 luaL_loadbuffer 并挂钩）
//!   2. 创建一个 lua_State 并调用 luaL_loadbuffer（触发 detour）
//!   3. 打印自己的 PID（供外部创建共享内存）
//!
//! 它不加载 Unity、不碰游戏、不影响任何现有进程。

use std::ffi::c_void;
use windows::core::PCWSTR;
use windows::Win32::Foundation::HMODULE;
use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

unsafe fn sym<T>(h: HMODULE, n: &str) -> Option<T> {
    let c = std::ffi::CString::new(n).ok()?;
    let p = GetProcAddress(h, windows::core::PCSTR(c.as_ptr() as *const u8))?;
    Some(std::mem::transmute_copy(&p))
}

fn main() {
    let xlua_path = std::env::args().nth(1).unwrap_or_else(|| {
        String::from(
            r"F:\haowancheng\GuiLongchao\game\GuiLongchao_Data\Plugins\x86_64\xlua.dll",
        )
    });
    let hold_ms: u64 = std::env::args()
        .nth(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(20_000);

    println!("HOSTPID={}", std::process::id());
    println!("HOST: loading {xlua_path}");

    let dir = std::path::Path::new(&xlua_path)
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_default();
    let old = std::env::var("PATH").unwrap_or_default();
    std::env::set_var("PATH", format!("{dir};{old}"));

    let w = wide(&xlua_path);
    let h = match unsafe { LoadLibraryW(PCWSTR(w.as_ptr())) } {
        Ok(h) => {
            println!("HOST: xlua.dll loaded at {:p}", h.0);
            h
        }
        Err(e) => {
            println!("HOST: LoadLibraryW failed: {e}");
            std::process::exit(2);
        }
    };

    let newstate: unsafe extern "C" fn() -> *mut c_void = match unsafe { sym(h, "luaL_newstate") } {
        Some(f) => f,
        None => {
            println!("HOST: luaL_newstate not found");
            std::process::exit(3);
        }
    };
    let openlibs: unsafe extern "C" fn(*mut c_void) = match unsafe { sym(h, "luaL_openlibs") } {
        Some(f) => f,
        None => {
            println!("HOST: luaL_openlibs not found");
            std::process::exit(3);
        }
    };
    let load: unsafe extern "C" fn(*mut c_void, *const u8, usize, *const i8) -> i32 =
        match unsafe { sym(h, "luaL_loadbuffer") } {
            Some(f) => f,
            None => {
                println!("HOST: luaL_loadbuffer not found");
                std::process::exit(5);
            }
        };
    // 我们当前挂钩的是 luaL_loadstring（luaL_loadbuffer 的真实体含 RIP 相对寻址，
    // 无法安全复制到 trampoline）。宿主必须调用被挂钩的那个才有意义。
    let loadstr: unsafe extern "C" fn(*mut c_void, *const i8) -> i32 =
        match unsafe { sym(h, "luaL_loadstring") } {
            Some(f) => f,
            None => {
                println!("HOST: luaL_loadstring not found");
                std::process::exit(6);
            }
        };

    println!("HOST: symbols resolved, entry = {:p}", load as *const c_void);

    println!("HOST: waiting 3000ms for injection...");
    std::thread::sleep(std::time::Duration::from_millis(3000));

    let l = unsafe { newstate() };
    if l.is_null() {
        println!("HOST: luaL_newstate returned null");
        std::process::exit(4);
    }
    unsafe { openlibs(l) };
    println!("HOST: lua_State created at {l:p}");

    // 触发被挂钩的入口
    let s1 = std::ffi::CString::new("return 1+1").unwrap();
    let rc = unsafe { loadstr(l, s1.as_ptr()) };
    println!("HOST: luaL_loadstring #1 returned {rc} (0 = ok)");

    let s2 = std::ffi::CString::new("return 42").unwrap();
    let rc2 = unsafe { loadstr(l, s2.as_ptr()) };
    println!("HOST: luaL_loadstring #2 returned {rc2}");

    // 顺便也调一次 loadbuffer，确认它没被破坏
    let src = b"return 7";
    let name = std::ffi::CString::new("@hosttest_lb").unwrap();
    let rc3 = unsafe { load(l, src.as_ptr(), src.len(), name.as_ptr()) };
    println!("HOST: luaL_loadbuffer returned {rc3}");

    // ---- 探针：直接把 OO 的引导脚本喂给 Lua，看它能否编译 ----
    if std::env::args().any(|a| a == "--probe-script") {
        // 默认指向 lua/oo 目录：引导脚本 P0-E3 已按用途拆成多个片段，
        // 这里按文件名顺序拼接，和 crates/oo_internal/build.rs 的拼法一致。
        let script_path = std::env::args()
            .skip_while(|a| a != "--probe-script")
            .nth(1)
            .unwrap_or_else(|| String::from("../../lua/oo"));
        println!("\nHOST-PROBE: compiling {script_path}");
        let script_body = {
            let p = std::path::Path::new(&script_path);
            if p.is_dir() {
                let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(p)
                    .map(|rd| {
                        rd.filter_map(|e| e.ok().map(|e| e.path()))
                            .filter(|q| {
                                q.extension().and_then(|s| s.to_str()) == Some("lua")
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                files.sort();
                let mut buf = String::new();
                for q in &files {
                    if let Ok(t) = std::fs::read_to_string(q) {
                        buf.push_str(&t);
                        if !buf.ends_with('\n') {
                            buf.push('\n');
                        }
                    }
                }
                buf
            } else {
                std::fs::read_to_string(p).unwrap_or_default()
            }
        };
        match Ok::<String, std::io::Error>(script_body) {
            Ok(body) => {
                println!("HOST-PROBE: {} bytes", body.len());
                let cs = std::ffi::CString::new(body.clone()).unwrap();
                let rc = unsafe { loadstr(l, cs.as_ptr()) };
                println!("HOST-PROBE: luaL_loadstring -> {rc}");

                // 编译失败时把 Lua 栈顶的错误信息取出来
                if rc != 0 {
                    let tolstring: unsafe extern "C" fn(*mut c_void, i32, *mut usize) -> *const i8 =
                        match unsafe { sym(h, "lua_tolstring") } {
                            Some(f) => f,
                            None => {
                                println!("HOST-PROBE: lua_tolstring missing");
                                std::process::exit(7);
                            }
                        };
                    let mut len = 0usize;
                    let msg = unsafe { tolstring(l, -1, &mut len) };
                    if msg.is_null() {
                        println!("HOST-PROBE: (no error message on stack)");
                    } else {
                        let b = unsafe { std::slice::from_raw_parts(msg as *const u8, len) };
                        println!(
                            "HOST-PROBE: LUA ERROR = {}",
                            String::from_utf8_lossy(b)
                        );
                    }
                } else {
                    println!("HOST-PROBE: 脚本编译通过");
                }
            }
            Err(e) => println!("HOST-PROBE: 读取失败 {e}"),
        }
    }

    println!("HOST: holding {hold_ms}ms for log inspection");
    std::thread::sleep(std::time::Duration::from_millis(hold_ms));
    println!("HOST: done");
}
