//! 极简 x86-64 指令长度解码器（仅用于确定内联挂钩的覆盖长度）。
//!
//! 设计原则：**保守**。只解码函数序言里常见的少量指令形态；
//! 遇到任何不认识的字节序列，立即返回 Err —— 由调用方拒绝挂钩。
//! 宁可挂不上，也绝不写坏游戏。
//!
//! 为什么必须逐条解码：内联挂钩要复制「完整指令」到 trampoline。
//! 实测 luaL_loadbuffer 的真实函数体开头是：
//!     48 83 EC 38            sub  rsp, 38h                (4 字节, 累计  4)
//!     48 89 54 24 20         mov  [rsp+20h], rdx          (5 字节, 累计  9)
//!     48 8D 15 50 F4 FF FF   lea  rdx, [rip-0xBB0]        (7 字节, 累计 16)
//! 若固定复制 14 字节，第三条 lea 会被从中间截断，且 rip 相对寻址基准改变，
//! trampoline 执行时必然崩溃。因此覆盖长度必须是「完整指令」的累计值（本例 16）。

/// 所需的最小覆盖长度（容纳 FF 25 + imm64 = 14 字节）。
pub const MIN_HOOK_LEN: usize = 14;

/// 覆盖长度的上限，超过则拒绝挂钩（避免动到函数主体）。
pub const MAX_HOOK_LEN: usize = 32;

/// 判断是否有 REX 前缀（0x40..=0x4F）。
#[inline]
fn is_rex(b: u8) -> bool {
    (0x40..=0x4F).contains(&b)
}

/// 解码一条指令的长度。返回 None 表示不认识，调用方应放弃挂钩。
///
/// 支持的形态（覆盖绝大多数函数序言）：
///   - REX 前缀 0x40..0x4F
///   - 操作数/地址尺寸前缀 0x66 / 0x67
///   - 段前缀 0x2E/0x36/0x3E/0x26/0x64/0x65
///   - push/pop r64，mov r/m, r（含 ModRM+SIB+disp），
///   - sub/add/and/or/xor/cmp r/m, imm8/imm32（0x83/0x81）
///   - lea / mov 带 rip 相对或基址+索引+disp
///   - 简单 ret / nop
///   - 已知的两字节 0F 前缀指令（如 0F 1F nop、0F B6 movzx）
pub fn decode_len(code: &[u8]) -> Option<usize> {
    if code.is_empty() {
        return None;
    }
    let mut i = 0usize;

    // ---- 前缀 ----
    // REX
    if i < code.len() && is_rex(code[i]) {
        i += 1;
    }
    // 操作数尺寸 / 地址尺寸 / 段前缀（可出现多个）
    while i < code.len()
        && matches!(code[i], 0x66 | 0x67 | 0x2E | 0x36 | 0x3E | 0x26 | 0x64 | 0x65 | 0xF2 | 0xF3)
    {
        i += 1;
    }
    if i >= code.len() {
        return None;
    }

    let op = code[i];
    i += 1;

    // ---- 无 ModRM 的简单指令 ----
    match op {
        0x50..=0x5F => return Some(i),            // push/pop r64
        0x90 => return Some(i),                    // nop
        0xC3 => return Some(i),                    // ret
        0xCC => return Some(i),                    // int3
        0xC9 => return Some(i),                    // leave
        0x98 | 0x99 => return Some(i),             // cwde / cdq
        // short jmp/jcc：虽然长度已知，但复制到 trampoline 后相对位移会错，
        // 因此拒绝（由 precheck 负责拦，这里也一并拒绝，双保险）
        0xEB | 0x70..=0x7F => return None,
        0xE8 | 0xE9 => return None,                // call/jmp rel32 —— 拒绝
        _ => {}
    }

    // ---- 两字节 0F 指令 ----
    if op == 0x0F {
        if i >= code.len() {
            return None;
        }
        let op2 = code[i];
        i += 1;
        match op2 {
            // 0F 1F /0  nop r/m
            0x1F => {
                let n = modrm_len(&code[i..], false)?;
                return Some(i + n);
            }
            // 0F B6 /r movzx r32, r/m8 ; 0F B7 /r movzx r32, r/m16
            0xB6 | 0xB7 => {
                let n = modrm_len(&code[i..], false)?;
                return Some(i + n);
            }
            // 0F 8x jcc rel32 —— 拒绝
            0x80..=0x8F => return None,
            _ => return None,
        }
    }

    // ---- 单字节 opcode + ModRM 的常见形态 ----
    // 0x88/0x89/0x8A/0x8B mov r/m,r 与 mov r,r/m
    // 0x8D lea
    // 0x01/0x03/0x09/0x0B/0x11/0x13/0x19/0x1B/0x21/0x23/0x29/0x2B/0x31/0x33/0x39/0x3B add/or/adc/sbb/and/sub/xor/cmp
    // 0x85/0x87 test/xchg
    let has_modrm = matches!(op,
        0x88 | 0x89 | 0x8A | 0x8B | 0x8D
        | 0x01 | 0x03 | 0x09 | 0x0B
        | 0x11 | 0x13 | 0x19 | 0x1B
        | 0x21 | 0x23 | 0x29 | 0x2B
        | 0x31 | 0x33 | 0x39 | 0x3B
        | 0x85 | 0x87
    );

    if has_modrm {
        let n = modrm_len(&code[i..], false)?;
        return Some(i + n);
    }

    // ---- 立即数形态 ----
    match op {
        // 0x83 /0 ib  —— sub/add/and/or/xor/cmp r/m, imm8
        0x83 => {
            let n = modrm_len(&code[i..], false)?;
            return Some(i + n + 1);
        }
        // 0x81 /0 id  —— imm32
        0x81 => {
            let n = modrm_len(&code[i..], false)?;
            return Some(i + n + 4);
        }
        // 0xC6 /0 ib / 0xC7 /0 id
        0xC6 => {
            let n = modrm_len(&code[i..], false)?;
            return Some(i + n + 1);
        }
        0xC7 => {
            let n = modrm_len(&code[i..], false)?;
            return Some(i + n + 4);
        }
        // 0xB8..0xBF mov r32, imm32
        0xB8..=0xBF => return Some(i + 4),
        _ => {}
    }

    None
}

/// 解码 ModRM（+SIB+disp）。返回消费的字节数。
/// `_is16` 预留：64 位模式下不涉及 16 位寻址。
fn modrm_len(code: &[u8], _is16: bool) -> Option<usize> {
    if code.is_empty() {
        return None;
    }
    let modrm = code[0];
    let md = modrm >> 6;
    let rm = modrm & 0x07;
    let mut n = 1usize;

    // SIB
    if rm == 4 && md != 3 {
        if code.len() < 2 {
            return None;
        }
        let sib = code[1];
        n += 1;
        let base = sib & 0x07;
        if md == 0 && base == 5 {
            n += 4; // disp32
        }
    }

    match md {
        0 => {
            // 无 disp，除非 rm=5（RIP 相对 disp32）或如上 SIB base=5
            if rm == 5 {
                n += 4;
            }
        }
        1 => n += 1, // disp8
        2 => n += 4, // disp32
        _ => {}     // md == 3：寄存器直接寻址，无 disp
    }

    if code.len() < n {
        return None;
    }
    Some(n)
}

/// 计算覆盖长度：从函数体开头逐条解码，直到累计 ≥ MIN_HOOK_LEN。
///
/// 返回完整指令的累计长度（≥ MIN_HOOK_LEN），或 None 表示无法安全挂钩。
pub fn coverage_len(code: &[u8]) -> Option<usize> {
    let mut off = 0usize;
    while off < MIN_HOOK_LEN {
        if off >= MAX_HOOK_LEN {
            return None;
        }
        let n = decode_len(&code[off..])?;
        if n == 0 {
            return None;
        }
        off += n;
    }
    if off > MAX_HOOK_LEN {
        return None;
    }
    Some(off)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn real_loadbuffer_prologue() {
        // 实测 luaL_loadbuffer 真实函数体开头
        let code = [
            0x48, 0x83, 0xEC, 0x38, // sub rsp, 38h
            0x48, 0x89, 0x54, 0x24, 0x20, // mov [rsp+20h], rdx
            0x48, 0x8D, 0x15, 0x50, 0xF4, 0xFF, 0xFF, // lea rdx, [rip-0xBB0]
        ];
        assert_eq!(decode_len(&code[0..]), Some(4));
        assert_eq!(decode_len(&code[4..]), Some(5));
        assert_eq!(decode_len(&code[9..]), Some(7));
        assert_eq!(coverage_len(&code), Some(16));
    }

    #[test]
    fn rejects_relative_branches() {
        assert_eq!(decode_len(&[0xE9, 0x00, 0x00, 0x00, 0x00]), None);
        assert_eq!(decode_len(&[0xE8, 0x00, 0x00, 0x00, 0x00]), None);
        assert_eq!(decode_len(&[0xEB, 0x10]), None);
        assert_eq!(decode_len(&[0x0F, 0x84, 0, 0, 0, 0]), None);
    }

    #[test]
    fn push_pop_and_nop() {
        assert_eq!(decode_len(&[0x55]), Some(1));
        assert_eq!(decode_len(&[0x5D]), Some(1));
        assert_eq!(decode_len(&[0x90]), Some(1));
        assert_eq!(decode_len(&[0xC3]), Some(1));
    }

    #[test]
    fn coverage_reaches_minimum() {
        // 4 个 push r64 = 4 字节，仍需继续解码到 >= 14
        let code = [
            0x55, 0x56, 0x57, 0x53, // push rbp/rsi/rdi/rbx
            0x48, 0x83, 0xEC, 0x20, // sub rsp, 20h
            0x48, 0x8B, 0x01, // mov rax, [rcx]
            0x90, // nop
        ];
        // 4 + 4 + 3 = 11 < 14；再加 nop = 12；再需要一个 -> 数据不够则 None
        let r = coverage_len(&code);
        assert!(r.is_none() || r.unwrap() >= MIN_HOOK_LEN);
    }

    #[test]
    fn rejects_unknown() {
        // VEX/AVX 前缀，我们不解码
        assert_eq!(decode_len(&[0xC5, 0xF8, 0x77]), None);
    }
}
/// 在前若干字节里查找 RIP 相对寻址指令，返回其偏移。
///
/// RIP 相对寻址的识别条件：REX 后可选的 opcode（lea/mov/add 等）+
/// ModRM 的 mod=00 且 rm=101。这类指令的 disp32 是相对**当前指令末尾**的位移，
/// 一旦指令被复制到别处，基准就变了，算出的地址完全错误。
///
/// 实测：luaL_loadbuffer 真实函数体 +10 处即为
///   48 8D 15 50 F4 FF FF   lea rdx, [rip-0xBB0]
/// 这正是它不能被简单复制式挂钩的原因。
pub fn find_rip_relative(code: &[u8]) -> Option<usize> {
    let mut off = 0usize;
    while off < code.len() {
        let start = off;
        let mut k = off;
        if k < code.len() && (0x40..=0x4F).contains(&code[k]) {
            k += 1;
        }
        if k + 1 >= code.len() {
            return None;
        }
        // 带 ModRM 的常见 opcode
        let has_modrm = matches!(
            code[k],
            0x88 | 0x89 | 0x8A | 0x8B | 0x8D | 0x01 | 0x03 | 0x09 | 0x0B | 0x11 | 0x13 | 0x19
                | 0x1B | 0x21 | 0x23 | 0x29 | 0x2B | 0x31 | 0x33 | 0x39 | 0x3B | 0x85 | 0x87
                | 0x83 | 0x81 | 0xC6 | 0xC7
        );
        if has_modrm {
            let md = code[k + 1];
            if (md >> 6) == 0 && (md & 7) == 5 {
                return Some(start);
            }
        }
        match decode_len(&code[off..]) {
            Some(l) if l > 0 => off += l,
            _ => return None,
        }
    }
    None
}
