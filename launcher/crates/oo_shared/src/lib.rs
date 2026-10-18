//! OpenOrange 启动器 / 注入 DLL 之间的共享契约层。
//!
//! 本 crate 被两个进程同时使用：
//!   * OO_Launcher.exe  —— 写端（服务端返回的登录结果）
//!   * OO_Internal.dll  —— 读端（游戏进程内，通过 PC_Internal_Result 暴露给 Lua）
//!
//! 通道采用 Windows 命名共享内存（Local\OO_InternalPC-<pid>），
//! 与闭源私服 InternalPC.dll 的 OpenFileMappingA 方案等价：
//! 游戏进程内不产生任何网络行为，账号身份完全由启动器投递。

use serde::{Deserialize, Serialize};

/// 共享内存名称前缀。真实名字会在后面拼接游戏进程 PID。
pub const CHANNEL_PREFIX: &str = r"Local\OO_InternalPC-";

/// 共享内存布局版本。两侧不一致时 DLL 拒绝工作并给出明确日志。
pub const BRIDGE_VERSION: u32 = 1;

/// 共享内存区块总大小（字节）。结果 JSON 上限约 60KB，足够任何服务端响应。
pub const BRIDGE_CAPACITY: usize = 64 * 1024;

/// 结果就绪标志：0 = 未就绪，1 = 已就绪。
pub const STATE_PENDING: u32 = 0;
pub const STATE_READY: u32 = 1;

/// 启动器投递给游戏进程的登录结果。
///
/// 字段与官方 login 响应保持同构：
///   { code: 1, msg: "Success", data: { account_id, ticket, session_key, ... } }
/// 其中 `data` 原样透传服务端返回，不做任何改写，
/// 以保证 fpxaccount.lua 的 `result.data.account_id` / `result.data.ticket` 可取到值。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoginEnvelope {
    pub code: i64,
    pub msg: String,
    pub data: serde_json::Value,
}

impl LoginEnvelope {
    pub fn ok(data: serde_json::Value) -> Self {
        Self { code: 1, msg: "Success".to_string(), data }
    }

    pub fn err(code: i64, msg: impl Into<String>) -> Self {
        Self { code, msg: msg.into(), data: serde_json::Value::Object(Default::default()) }
    }

    pub fn is_ok(&self) -> bool {
        self.code == 1
    }

    /// 序列化为投递给 DLL 的 JSON 字符串。
    pub fn to_payload(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| {
            r#"{"code":-1,"msg":"serialize failed","data":{}}"#.to_string()
        })
    }
}

/// 共享内存区块头（定长，位于区块起始处）。
///
/// 布局（小端）：
///   0x000  magic      u32   = 0x4F4F4950 ("OOIP")
///   0x004  version    u32
///   0x008  state      u32   0=pending 1=ready
///   0x00C  reserved   u32
///   0x010  length     u64   payload 字节数
///   0x018  reserved2  u64
///   0x020  payload    [u8; BRIDGE_CAPACITY - 0x20]
#[repr(C)]
#[derive(Debug, Clone, Copy)]
pub struct BridgeHeader {
    pub magic: u32,
    pub version: u32,
    pub state: u32,
    pub reserved: u32,
    pub length: u64,
    pub reserved2: u64,
}

pub const BRIDGE_MAGIC: u32 = 0x4F4F_4950; // "OOIP"
pub const BRIDGE_HEADER_SIZE: usize = std::mem::size_of::<BridgeHeader>();
pub const BRIDGE_PAYLOAD_CAPACITY: usize = BRIDGE_CAPACITY - BRIDGE_HEADER_SIZE;

impl Default for BridgeHeader {
    fn default() -> Self {
        Self {
            magic: BRIDGE_MAGIC,
            version: BRIDGE_VERSION,
            state: STATE_PENDING,
            reserved: 0,
            length: 0,
            reserved2: 0,
        }
    }
}

/// 构造指定游戏进程 PID 的共享内存名称。
pub fn channel_name(pid: u32) -> String {
    format!("{CHANNEL_PREFIX}{pid}")
}

/// 内容寻址的脚本哈希：用于确认 DLL 内嵌脚本与 lua/ 下的一致。
pub fn script_fingerprint(src: &str) -> u64 {
    // FNV-1a 64，纯用于一致性自检，不用于安全用途。
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in src.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01B3);
    }
    h
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_roundtrip() {
        let env = LoginEnvelope::ok(serde_json::json!({
            "account_id": "oo_f5938e982e1d3cf0",
            "ticket": "abcdef",
        }));
        let s = env.to_payload();
        let back: LoginEnvelope = serde_json::from_str(&s).unwrap();
        assert_eq!(back.code, 1);
        assert!(back.is_ok());
        assert_eq!(back.data["account_id"], "oo_f5938e982e1d3cf0");
    }

    #[test]
    fn header_layout_is_stable() {
        assert_eq!(BRIDGE_HEADER_SIZE, 32);
        assert!(BRIDGE_PAYLOAD_CAPACITY > 60 * 1024);
    }

    #[test]
    fn channel_name_shape() {
        assert_eq!(channel_name(1234), r"Local\OO_InternalPC-1234");
    }

    #[test]
    fn fingerprint_is_deterministic() {
        assert_eq!(script_fingerprint("abc"), script_fingerprint("abc"));
        assert_ne!(script_fingerprint("abc"), script_fingerprint("abd"));
    }
}

// ============================================================================
// 磁盘兜底通道
// ============================================================================
//
// 为什么需要它：共享内存的生命周期绑定在**创建它的进程**上。
// 启动器拉起游戏后若退出（CLI 模式跑完即退，或用户关掉窗口），
// Windows 回收其句柄 -> 映射对象销毁 -> 游戏内 DLL 再 OpenFileMappingW 必然失败。
// 实测：启动器写入 Local\OO_InternalPC-23088 成功（1095 字节），
//       但游戏 36.6 秒后（xlua 加载完）去读时得到
//       "系统找不到指定的文件 (0x80070002)"，导致 login result not ready。
//
// 因此把同一份 JSON 同时落盘到游戏目录，DLL 优先读共享内存、失败则读文件，
// 彻底摆脱进程生命周期约束。

/// 磁盘兜底文件名（写在游戏可执行文件同目录）。
pub const DISK_FALLBACK_NAME: &str = "oo_login.json";

/// 把登录载荷写入磁盘兜底文件。
pub fn write_disk_payload(dir: &std::path::Path, payload: &str) -> std::io::Result<std::path::PathBuf> {
    let path = dir.join(DISK_FALLBACK_NAME);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, payload)?;
    Ok(path)
}

/// 从磁盘兜底文件读取登录载荷。
pub fn read_disk_payload(dir: &std::path::Path) -> Option<String> {
    let path = dir.join(DISK_FALLBACK_NAME);
    std::fs::read_to_string(path).ok()
}

#[cfg(test)]
mod disk_tests {
    use super::*;

    #[test]
    fn disk_roundtrip() {
        let dir = std::env::temp_dir().join("oo_disk_test");
        let _ = std::fs::create_dir_all(&dir);
        let env = LoginEnvelope::ok(serde_json::json!({"account_id": "acc1", "ticket": "tk"}));
        let payload = env.to_payload();
        write_disk_payload(&dir, &payload).expect("write");
        let back = read_disk_payload(&dir).expect("read");
        assert_eq!(back, payload);
        let parsed: LoginEnvelope = serde_json::from_str(&back).expect("parse");
        assert_eq!(parsed.code, 1);
        assert_eq!(parsed.data.get("account_id").unwrap().as_str().unwrap(), "acc1");
    }
}
