//! oo_keyfinder —— UnityCN AssetBundle 密钥搜索
//!
//! 原理：UnityCN 加密的包里能读到两组「已知明文」：
//!   key_sig  —— 索引区里被加密的 16 字节
//!   data_sig —— 与之对应的 16 字节
//! 客户端解密的验证条件是：
//!   AES128_ECB(key, key_sig) XOR data_sig == b"#$unity3dchina!@"
//! 于是把 AES 期望输出预先算出来：
//!   target = data_sig XOR b"#$unity3dchina!@"
//! 之后只要找满足 AES128_ECB(k, key_sig) == target 的 16 字节 k 即可。
//!
//! 用法：
//!   oo_keyfinder --key-sig <hex> --data-sig <hex> --file <路径>
//!   oo_keyfinder --key-sig <hex> --data-sig <hex> --pid <pid|auto>
//!
//! --pid auto 会自动找 GuiLongchao.exe。

use aes::cipher::{BlockEncrypt, KeyInit};
use aes::Aes128;
use std::env;
use std::fs;

const SIGNATURE: &[u8; 16] = b"#$unity3dchina!@";

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    let s: String = s.chars().filter(|c| !c.is_whitespace() && *c != ',').collect();
    if s.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        let hi = (b[i] as char).to_digit(16)?;
        let lo = (b[i + 1] as char).to_digit(16)?;
        out.push((hi * 16 + lo) as u8);
        i += 2;
    }
    Some(out)
}

fn hex_encode(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

/// 在缓冲区里逐字节滑动找密钥
fn scan(buf: &[u8], key_sig: &[u8; 16], target: &[u8; 16]) -> Option<usize> {
    if buf.len() < 16 {
        return None;
    }
    let mut block = aes::Block::clone_from_slice(key_sig);
    let last = buf.len() - 16;
    for i in 0..=last {
        let k = &buf[i..i + 16];
        let cipher = match Aes128::new_from_slice(k) {
            Ok(c) => c,
            Err(_) => continue,
        };
        block.copy_from_slice(key_sig);
        cipher.encrypt_block(&mut block);
        if &block[..] == &target[..] {
            return Some(i);
        }
    }
    None
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let mut key_sig_hex = None;
    let mut data_sig_hex = None;
    let mut file = None;
    let mut pid_arg = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--key-sig" => {
                key_sig_hex = args.get(i + 1).cloned();
                i += 2;
            }
            "--data-sig" => {
                data_sig_hex = args.get(i + 1).cloned();
                i += 2;
            }
            "--file" => {
                file = args.get(i + 1).cloned();
                i += 2;
            }
            "--pid" => {
                pid_arg = args.get(i + 1).cloned();
                i += 2;
            }
            "-h" | "--help" => {
                print_help();
                return;
            }
            other => {
                eprintln!("未知参数: {}", other);
                print_help();
                std::process::exit(2);
            }
        }
    }

    let key_sig_v = match key_sig_hex.as_deref().and_then(hex_decode) {
        Some(v) if v.len() == 16 => v,
        _ => {
            eprintln!("需要 --key-sig（32 位十六进制）");
            std::process::exit(2);
        }
    };
    let data_sig_v = match data_sig_hex.as_deref().and_then(hex_decode) {
        Some(v) if v.len() == 16 => v,
        _ => {
            eprintln!("需要 --data-sig（32 位十六进制）");
            std::process::exit(2);
        }
    };

    let mut key_sig = [0u8; 16];
    key_sig.copy_from_slice(&key_sig_v);
    let mut target = [0u8; 16];
    for n in 0..16 {
        target[n] = data_sig_v[n] ^ SIGNATURE[n];
    }

    println!("key_sig = {}", hex_encode(&key_sig));
    println!("target  = {}", hex_encode(&target));

    if let Some(path) = file {
        scan_file(&path, &key_sig, &target);
    } else if let Some(p) = pid_arg {
        scan_process(&p, &key_sig, &target);
    } else {
        eprintln!("需要 --file 或 --pid");
        std::process::exit(2);
    }
}

fn scan_file(path: &str, key_sig: &[u8; 16], target: &[u8; 16]) {
    let t0 = std::time::Instant::now();
    let data = match fs::read(path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("读取失败 {}: {}", path, e);
            return;
        }
    };
    println!("扫描文件 {} ({:.1} MB)", path, data.len() as f64 / 1048576.0);
    match scan(&data, key_sig, target) {
        Some(off) => {
            println!("*** 找到密钥 (文件偏移 0x{:X}): {} ***", off, hex_encode(&data[off..off + 16]));
        }
        None => println!("未找到（用时 {:.1}s）", t0.elapsed().as_secs_f64()),
    }
}

fn print_help() {
    println!("oo_keyfinder —— UnityCN AssetBundle 密钥搜索");
    println!();
    println!("用法:");
    println!("  oo_keyfinder --key-sig <hex> --data-sig <hex> --file <路径>");
    println!("  oo_keyfinder --key-sig <hex> --data-sig <hex> --pid <pid|auto>");
}

// ---------------------------------------------------------------- 进程内存
#[cfg(windows)]
fn scan_process(pid_arg: &str, key_sig: &[u8; 16], target: &[u8; 16]) {
    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::Diagnostics::Debug::ReadProcessMemory;
    use windows::Win32::System::Memory::{
        VirtualQueryEx, MEMORY_BASIC_INFORMATION, MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS,
        PAGE_PROTECTION_FLAGS, PAGE_READONLY, PAGE_READWRITE, PAGE_WRITECOPY,
        PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
    };
    use windows::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    };

    let pid: u32 = if pid_arg.eq_ignore_ascii_case("auto") {
        match find_pid("GuiLongchao.exe") {
            Some(p) => p,
            None => {
                eprintln!("没找到 GuiLongchao.exe 进程，请先把游戏跑起来");
                std::process::exit(3);
            }
        }
    } else {
        match pid_arg.parse() {
            Ok(p) => p,
            Err(_) => {
                eprintln!("pid 不合法: {}", pid_arg);
                std::process::exit(2);
            }
        }
    };

    println!("扫描进程 PID={}", pid);
    let handle: HANDLE = unsafe {
        match OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid) {
            Ok(h) => h,
            Err(e) => {
                eprintln!("OpenProcess 失败: {}（需要管理员权限）", e);
                std::process::exit(3);
            }
        }
    };

    let readable_mask = PAGE_READONLY
        | PAGE_READWRITE
        | PAGE_WRITECOPY
        | PAGE_EXECUTE_READ
        | PAGE_EXECUTE_READWRITE
        | PAGE_EXECUTE_WRITECOPY;

    let mut addr: usize = 0;
    let mut scanned: u64 = 0;
    let mut regions: u64 = 0;
    let t0 = std::time::Instant::now();
    let mut found = false;

    loop {
        let mut mbi = MEMORY_BASIC_INFORMATION::default();
        let ret = unsafe {
            VirtualQueryEx(
                handle,
                Some(addr as *const core::ffi::c_void),
                &mut mbi,
                std::mem::size_of::<MEMORY_BASIC_INFORMATION>(),
            )
        };
        if ret == 0 {
            break;
        }
        let base = mbi.BaseAddress as usize;
        let size = mbi.RegionSize;
        if size == 0 {
            break;
        }

        let prot = mbi.Protect;
        let is_readable = mbi.State == MEM_COMMIT
            && (prot.0 & readable_mask.0) != 0
            && (prot.0 & PAGE_GUARD.0) == 0
            && (prot.0 & PAGE_NOACCESS.0) == 0;

        if is_readable {
            regions += 1;
            // 分块读取
            let mut off = 0usize;
            let chunk = 4 * 1024 * 1024;
            while off < size {
                let n = std::cmp::min(chunk, size - off);
                let mut buf = vec![0u8; n];
                let mut got = 0usize;
                let ok = unsafe {
                    ReadProcessMemory(
                        handle,
                        (base + off) as *const core::ffi::c_void,
                        buf.as_mut_ptr() as *mut core::ffi::c_void,
                        n,
                        Some(&mut got),
                    )
                };
                if ok.is_ok() && got >= 16 {
                    scanned += got as u64;
                    buf.truncate(got);
                    if let Some(idx) = scan(&buf, key_sig, target) {
                        println!(
                            "*** 找到密钥 @ 0x{:X}: {} ***",
                            base + off + idx,
                            hex_encode(&buf[idx..idx + 16])
                        );
                        found = true;
                        break;
                    }
                }
                off += n;
            }
            if found {
                break;
            }
            if regions % 200 == 0 {
                println!(
                    "  已扫 {} 个区域 / {:.0} MB / {:.1}s",
                    regions,
                    scanned as f64 / 1048576.0,
                    t0.elapsed().as_secs_f64()
                );
            }
        }

        let next = base.wrapping_add(size);
        if next <= addr {
            break;
        }
        addr = next;
    }

    unsafe {
        let _ = CloseHandle(handle);
    }
    println!(
        "扫描结束：{} 个区域，{:.1} MB，用时 {:.1}s，{}",
        regions,
        scanned as f64 / 1048576.0,
        t0.elapsed().as_secs_f64(),
        if found { "已找到" } else { "未找到" }
    );
    if !found {
        std::process::exit(1);
    }
}

#[cfg(windows)]
fn find_pid(name: &str) -> Option<u32> {
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::ProcessStatus::{
        EnumProcesses, GetModuleBaseNameW,
    };
    use windows::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ,
    };

    let mut pids = vec![0u32; 4096];
    let mut needed = 0u32;
    unsafe {
        if EnumProcesses(pids.as_mut_ptr(), (pids.len() * 4) as u32, &mut needed).is_err() {
            return None;
        }
    }
    let count = (needed / 4) as usize;
    let target = name.to_lowercase();
    for &p in &pids[..count] {
        if p == 0 {
            continue;
        }
        let h = unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, p) };
        let h = match h {
            Ok(h) => h,
            Err(_) => continue,
        };
        let mut buf = [0u16; 260];
        let n = unsafe { GetModuleBaseNameW(h, None, &mut buf) };
        unsafe {
            let _ = CloseHandle(h);
        }
        if n > 0 {
            let s = String::from_utf16_lossy(&buf[..n as usize]);
            if s.to_lowercase() == target {
                return Some(p);
            }
        }
    }
    None
}

#[cfg(not(windows))]
fn scan_process(_p: &str, _k: &[u8; 16], _t: &[u8; 16]) {
    eprintln!("进程内存扫描只支持 Windows");
}
