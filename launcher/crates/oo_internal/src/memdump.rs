//! 内存中 dump 已解密的 .NET 程序集。
//!
//! 背景：`Assembly-CSharp.dll` 等 16 个 HybridData 程序集在磁盘上是加密的
//! （首字节随机，不是 PE）。但 HybridCLR 运行时必须把它们解密后才能执行，
//! 所以**解密后的 PE 一定存在于进程内存里**。
//!
//! 本模块在自身进程内扫描可读内存，寻找形如 PE 的映像：
//!   - 以 "MZ" 开头
//!   - 0x3C 处的 e_lfanew 合理（< 0x1000）
//!   - e_lfanew 处为 "PE\0\0"
//!   - 可选：数据目录第 14 项（CLI Header）非零 —— 说明是 .NET 程序集
//! 然后把候选按"原始文件大小"（各节 PointerToRawData + SizeOfRawData 的最大值）落盘。
//!
//! 只用 ReadProcessMemory 读自身内存，不写、不挂钩、不改任何状态。

use std::ffi::c_void;

use windows::Win32::Foundation::HMODULE;
use windows::Win32::System::Diagnostics::Debug::ReadProcessMemory;
use windows::Win32::System::Memory::{
    VirtualQuery, MEMORY_BASIC_INFORMATION, MEM_COMMIT, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY, PAGE_GUARD, PAGE_NOACCESS, PAGE_READONLY, PAGE_READWRITE,
    PAGE_WRITECOPY,
};
use windows::Win32::System::Threading::GetCurrentProcess;

const MAX_REGION: usize = 128 * 1024 * 1024; // 单区域上限
const MAX_DUMP: usize = 64 * 1024 * 1024;    // 单文件上限

fn readable(protect: u32) -> bool {
    if protect & PAGE_GUARD.0 != 0 || protect & PAGE_NOACCESS.0 != 0 {
        return false;
    }
    let p = protect
        & (PAGE_READONLY.0
            | PAGE_READWRITE.0
            | PAGE_EXECUTE_READ.0
            | PAGE_EXECUTE_READWRITE.0
            | PAGE_WRITECOPY.0
            | PAGE_EXECUTE_WRITECOPY.0);
    p != 0
}

fn rd_u16(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}
fn rd_u32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}

/// 校验偏移 i 处是否是一个合法 PE，返回 (原始文件大小, 是否含 CLI 目录)
fn pe_at(buf: &[u8], i: usize) -> Option<(usize, bool)> {
    if i + 0x40 > buf.len() {
        return None;
    }
    if buf[i] != b'M' || buf[i + 1] != b'Z' {
        return None;
    }
    let e_lfanew = rd_u32(buf, i + 0x3C) as usize;
    if e_lfanew < 0x40 || e_lfanew > 0x1000 {
        return None;
    }
    let pe = i + e_lfanew;
    if pe + 0x18 + 0xF0 > buf.len() {
        return None;
    }
    if &buf[pe..pe + 4] != b"PE\0\0" {
        return None;
    }
    let coff = pe + 4;
    let num_sections = rd_u16(buf, coff + 2) as usize;
    let opt_size = rd_u16(buf, coff + 16) as usize;
    let opt = coff + 20;
    if opt + 2 > buf.len() {
        return None;
    }
    let magic = rd_u16(buf, opt);
    let (dir_off, n_dirs_off) = match magic {
        0x10B => (opt + 96, opt + 92),  // PE32
        0x20B => (opt + 112, opt + 108), // PE32+
        _ => return None,
    };
    if n_dirs_off + 4 > buf.len() {
        return None;
    }
    let n_dirs = rd_u32(buf, n_dirs_off) as usize;
    // CLI Header 是数据目录索引 14
    let has_cli = n_dirs > 14 && dir_off + 14 * 8 + 8 <= buf.len() && rd_u32(buf, dir_off + 14 * 8) != 0;

    // 原始文件大小 = max(PointerToRawData + SizeOfRawData)
    let sec = opt + opt_size;
    let mut raw_end = 0usize;
    for s in 0..num_sections {
        let sh = sec + s * 40;
        if sh + 40 > buf.len() {
            break;
        }
        let size_raw = rd_u32(buf, sh + 16) as usize;
        let ptr_raw = rd_u32(buf, sh + 20) as usize;
        let end = ptr_raw + size_raw;
        if end > raw_end {
            raw_end = end;
        }
    }
    if raw_end < 0x200 || raw_end > MAX_DUMP {
        return None;
    }
    Some((raw_end, has_cli))
}

/// 扫描自身进程内存，把找到的 PE 映像落盘。返回找到的候选数。
pub fn scan_and_dump(out_dir: &std::path::Path) -> usize {
    let proc = unsafe { GetCurrentProcess() };
    let mut addr = 0x10000usize;
    let mut found = 0usize;
    let mut dumped_sizes: Vec<(usize, u8)> = Vec::new();

    unsafe {
        loop {
            let mut mbi = MEMORY_BASIC_INFORMATION::default();
            let r = VirtualQuery(
                Some(addr as *const c_void),
                &mut mbi,
                std::mem::size_of::<MEMORY_BASIC_INFORMATION>(),
            );
            if r == 0 {
                break;
            }
            let base = mbi.BaseAddress as usize;
            let size = mbi.RegionSize;
            if size == 0 {
                break;
            }
            if mbi.State == MEM_COMMIT && size <= MAX_REGION && readable(mbi.Protect.0) {
                let mut buf = vec![0u8; size];
                let mut got = 0usize;
                let ok = ReadProcessMemory(
                    proc,
                    base as *const c_void,
                    buf.as_mut_ptr() as *mut c_void,
                    size,
                    Some(&mut got),
                )
                .is_ok();
                if ok && got > 0x200 {
                    buf.truncate(got);
                    let mut i = 0usize;
                    while i + 0x200 < buf.len() {
                        if buf[i] == b'M' && buf[i + 1] == b'Z' {
                            if let Some((raw_size, has_cli)) = pe_at(&buf, i) {
                                let avail = buf.len() - i;
                                if raw_size <= avail {
                                    let key = (raw_size, has_cli as u8);
                                    if !dumped_sizes.contains(&key) {
                                        dumped_sizes.push(key);
                                        let name = format!(
                                            "dump_{:03}_{}KB{}.bin",
                                            found,
                                            raw_size / 1024,
                                            if has_cli { "_net" } else { "" }
                                        );
                                        let path = out_dir.join(&name);
                                        match std::fs::write(&path, &buf[i..i + raw_size]) {
                                            Ok(_) => {
                                                crate::log::write(&format!(
                                                    "memdump: 写出 {} ({} 字节, CLI={})",
                                                    name, raw_size, has_cli
                                                ));
                                                found += 1;
                                            }
                                            Err(e) => {
                                                crate::log::write(&format!(
                                                    "memdump: 写 {} 失败: {e}",
                                                    name
                                                ));
                                            }
                                        }
                                    }
                                }
                                i += 0x1000;
                                continue;
                            }
                        }
                        i += 1;
                    }
                }
            }
            let next = base.saturating_add(size);
            if next <= addr {
                break;
            }
            addr = next;
            if addr > 0x7FFF_FFFF_FFFF {
                break;
            }
        }
    }
    let _ = HMODULE::default();
    found
}

// ============================================================================
// 按模块 dump 内存镜像（脱壳）
// ============================================================================
//
// 背景：xlua.dll 的 .text/.rdata/.data/.pdata/.idata/.tls/.00cfg/.200 共 8 个节
// 在磁盘上 PointerToRawData = SizeOfRawData = 0 —— 真实内容被压进 .201 节，
// 加载时才展开。所以直接对磁盘文件做静态反汇编是看不到代码的。
//
// 做法：把模块在内存里的展开镜像整段拷出来（SizeOfImage 字节），
// 再把节表里的 PointerToRawData/SizeOfRawData 改成与虚拟布局一致，
// 得到一个 Ghidra 能直接加载的"已脱壳 PE"。

use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::core::PCWSTR;

fn align_up(v: usize, a: usize) -> usize {
    if a == 0 { return v; }
    (v + a - 1) / a * a
}

pub fn dump_module_image(module_name: &str, out_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    let w: Vec<u16> = module_name.encode_utf16().chain(std::iter::once(0)).collect();
    let h = unsafe { GetModuleHandleW(PCWSTR(w.as_ptr())) }.ok()?;
    if h.0.is_null() {
        return None;
    }
    let base = h.0 as usize;

    unsafe {
        let dos = base as *const u8;
        if *dos != b'M' || *dos.add(1) != b'Z' {
            return None;
        }
        let e_lfanew = *(dos.add(0x3C) as *const u32) as usize;
        if e_lfanew < 0x40 || e_lfanew > 0x1000 {
            return None;
        }
        let nt = dos.add(e_lfanew);
        let sig = std::slice::from_raw_parts(nt, 4);
        if sig != b"PE\0\0" {
            return None;
        }
        let coff = nt.add(4);
        let num_sec = *(coff.add(2) as *const u16) as usize;
        let opt_size = *(coff.add(16) as *const u16) as usize;
        let opt = coff.add(20);
        let magic = *(opt as *const u16);
        if magic != 0x10B && magic != 0x20B {
            return None;
        }
        // OptionalHeader 偏移 56 = SizeOfImage（PE32 与 PE32+ 相同）
        let size_of_image = *(opt.add(56) as *const u32) as usize;
        if size_of_image == 0 || size_of_image > MAX_DUMP {
            return None;
        }

        // 整段拷出内存镜像
        let mut img = vec![0u8; size_of_image];
        std::ptr::copy_nonoverlapping(dos, img.as_mut_ptr(), size_of_image);

        // 修正节表：让 raw 布局等于虚拟布局
        let sec_off = (opt as usize - dos as usize) + opt_size;
        for i in 0..num_sec {
            let sh = sec_off + i * 40;
            if sh + 40 > img.len() {
                break;
            }
            let vsize = u32::from_le_bytes([img[sh + 8], img[sh + 9], img[sh + 10], img[sh + 11]]) as usize;
            let vaddr = u32::from_le_bytes([img[sh + 12], img[sh + 13], img[sh + 14], img[sh + 15]]) as usize;
            let rsize = u32::from_le_bytes([img[sh + 16], img[sh + 17], img[sh + 18], img[sh + 19]]) as usize;
            let use_size = if vsize > 0 { vsize } else { rsize };
            if use_size == 0 {
                continue;
            }
            let new_raw = align_up(use_size, 0x200) as u32;
            let new_ptr = vaddr as u32;
            img[sh + 16..sh + 20].copy_from_slice(&new_raw.to_le_bytes());
            img[sh + 20..sh + 24].copy_from_slice(&new_ptr.to_le_bytes());
        }

        let safe: String = module_name
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || c == '.' { c } else { '_' })
            .collect();
        let path = out_dir.join(format!("{}_unpacked.bin", safe));
        match std::fs::write(&path, &img) {
            Ok(_) => {
                crate::log::write(&format!(
                    "memdump: 已脱壳 {} -> {} ({} 字节, SizeOfImage=0x{:X}, 节数={})",
                    module_name,
                    path.display(),
                    size_of_image,
                    size_of_image,
                    num_sec
                ));
                Some(path)
            }
            Err(e) => {
                crate::log::write(&format!("memdump: 写 {} 失败: {e}", path.display()));
                None
            }
        }
    }
}
