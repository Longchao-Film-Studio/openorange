//! 指令分析诊断工具（供 oo_selfcheck 复用，消除与 insn.rs 的双份维护）。
//!
//! 背景（2026-10-24 拆分）：selfcheck/main.rs 曾手工复制一份「本地精简版指令解码」
//! （local_decode/local_modrm/local_coverage/local_rip_rel + analyze_reloc），
//! 与本 crate 的 insn.rs 是同一逻辑两处维护（R3 违规）——insn.rs 修 bug 时那边不会同步。
//! 现统一收敛到这里，selfcheck 通过 `oo_internal::diag::*` 调用。
//!
//! 注意：这些是「诊断用的精简解码」（覆盖常见 il2cpp 序言），
//! 与 insn.rs 的运行时严格版（decode_len/coverage_len）**语义一致但实现独立**——
//! 诊断器故意用简化实现来交叉验证运行时版的判断。

/// 分析前 24 字节，返回阻碍重定位的因素（相对跳转 / RIP 相对寻址）。
pub fn analyze_reloc(code: &[u8]) -> Vec<String> {
    let mut risks = Vec::new();
    let mut i = 0usize;
    while i < 20 {
        if i + 1 >= code.len() { break; }
        let b = code[i];
        if b == 0xE8 || b == 0xE9 || b == 0xEB {
            risks.push(format!("相对跳转 {b:02X} at +{i}"));
            break;
        }
        if b == 0x0F && (code[i + 1] & 0xF0) == 0x80 {
            risks.push(format!("jcc rel32 at +{i}"));
            break;
        }
        // lea/mov 带 RIP 相对：REX(可选) 8D/8B/89/8A/88 + ModRM mod=00 rm=101
        let mut k = i;
        if (0x40..=0x4F).contains(&code[k]) {
            k += 1;
        }
        if k + 1 >= code.len() { break; }
        if matches!(code[k], 0x8D | 0x8B | 0x89 | 0x8A | 0x88 | 0x03 | 0x01) {
            let md = code[k + 1];
            if (md >> 6) == 0 && (md & 7) == 5 {
                risks.push(format!("RIP 相对寻址 at +{k}"));
                break;
            }
        }
        i += 1;
    }
    risks
}

/// 精简版指令长度解码（诊断用，见文件头说明）。
pub fn decode_len_lite(code: &[u8]) -> Option<usize> {
    if code.is_empty() { return None; }
    let mut i = 0usize;
    if (0x40..=0x4F).contains(&code[i]) { i += 1; }
    while i < code.len() && matches!(code[i], 0x66 | 0x67 | 0x2E | 0x36 | 0x3E | 0x26 | 0x64 | 0x65 | 0xF2 | 0xF3) { i += 1; }
    if i >= code.len() { return None; }
    let op = code[i]; i += 1;
    match op {
        0x50..=0x5F | 0x90 | 0xC3 | 0xCC | 0xC9 | 0x98 | 0x99 => return Some(i),
        0xEB | 0x70..=0x7F | 0xE8 | 0xE9 => return None,
        _ => {}
    }
    if op == 0x0F {
        if i >= code.len() { return None; }
        let op2 = code[i]; i += 1;
        return match op2 {
            0x1F | 0xB6 | 0xB7 => modrm_len(&code[i..]).map(|n| i + n),
            _ => None,
        };
    }
    let has_modrm = matches!(op,
        0x88|0x89|0x8A|0x8B|0x8D|0x01|0x03|0x09|0x0B|0x11|0x13|0x19|0x1B
        |0x21|0x23|0x29|0x2B|0x31|0x33|0x39|0x3B|0x85|0x87);
    if has_modrm { return modrm_len(&code[i..]).map(|n| i + n); }
    match op {
        0x83 => modrm_len(&code[i..]).map(|n| i + n + 1),
        0x81 => modrm_len(&code[i..]).map(|n| i + n + 4),
        0xC6 => modrm_len(&code[i..]).map(|n| i + n + 1),
        0xC7 => modrm_len(&code[i..]).map(|n| i + n + 4),
        0xB8..=0xBF => Some(i + 4),
        _ => None,
    }
}

fn modrm_len(code: &[u8]) -> Option<usize> {
    if code.is_empty() { return None; }
    let md = code[0] >> 6; let rm = code[0] & 7; let mut n = 1usize;
    if rm == 4 && md != 3 {
        if code.len() < 2 { return None; }
        n += 1;
        if md == 0 && (code[1] & 7) == 5 { n += 4; }
    }
    match md { 0 => { if rm == 5 { n += 4; } }, 1 => n += 1, 2 => n += 4, _ => {} }
    if code.len() < n { return None; }
    Some(n)
}

/// 覆盖 HOOK_LEN(14) 字节的完整指令边界长度。
pub fn coverage_len_lite(code: &[u8]) -> Option<usize> {
    let mut off = 0usize;
    while off < 14 {
        if off >= 32 { return None; }
        let n = decode_len_lite(&code[off..])?;
        if n == 0 { return None; }
        off += n;
    }
    Some(off)
}

/// 找第一个 RIP 相对寻址的指令偏移。
pub fn find_rip_rel_lite(code: &[u8]) -> Option<usize> {
    let mut off = 0usize;
    while off < code.len() {
        let start = off;
        let mut k = off;
        if k < code.len() && (0x40..=0x4F).contains(&code[k]) { k += 1; }
        if k + 1 >= code.len() { return None; }
        let has_modrm = matches!(code[k],
            0x88|0x89|0x8A|0x8B|0x8D|0x01|0x03|0x09|0x0B|0x11|0x13|0x19|0x1B
            |0x21|0x23|0x29|0x2B|0x31|0x33|0x39|0x3B|0x85|0x87
            |0x83|0x81|0xC6|0xC7);
        if has_modrm {
            let md = code[k + 1];
            if (md >> 6) == 0 && (md & 7) == 5 { return Some(start); }
        }
        match decode_len_lite(&code[off..]) {
            Some(l) if l > 0 => off += l,
            _ => return None,
        }
    }
    None
}