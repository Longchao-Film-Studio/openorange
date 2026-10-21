//! 内嵌的引导 Lua 脚本。
//!
//! 源码在 launcher/lua/oo/*.lua（按用途拆成 6 个片段），构建时由 build.rs
//! 按文件名顺序拼成一个 chunk 写进 OUT_DIR，再用 include_str! 编译进 DLL。
//! 因此发布时只需 OO_Launcher.exe + OO_Internal.dll + OpenOrange.ini 三个文件。

/// 引导脚本全文（由 build.rs 从 lua/oo/*.lua 生成）。
pub const BOOTSTRAP_LUA: &str = include_str!(concat!(env!("OUT_DIR"), "/oo_bootstrap.generated.lua"));

/// 由 DLL 注入到 _G 的原生函数名（脚本依赖它们）。
pub const FN_LOG: &str = "OO_Internal_Log";
pub const FN_REQUEST_LOGIN: &str = "OO_Internal_RequestLogin";
pub const FN_RESULT: &str = "OO_Internal_Result";
pub const FN_TRACKING: &str = "OO_Internal_TrackingInfo";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn script_is_embedded_and_nonempty() {
        assert!(
            BOOTSTRAP_LUA.len() > 1000,
            "script suspiciously short: {}",
            BOOTSTRAP_LUA.len()
        );
    }

    #[test]
    fn script_has_required_hooks() {
        for needle in [
            "modules.sdk.fpxsdkapi",
            "modules.sdk.fpxsdkreceiver",
            "ui.panel.loginpanel.loginpanel",
            "platform",
            "onSdkLogin",
            "EnterGame",
            "OO_Internal_RequestLogin",
            "OO_Internal_Result",
            "openServerSelectPanel",
            "BindGameTickFunc",
        ] {
            assert!(BOOTSTRAP_LUA.contains(needle), "script missing: {needle}");
        }
    }

    /// 不允许残留原版第三方面板/中转服的地址。
    ///
    /// ★ 判定方式：只看 URL 里的 host，不做「整篇不能出现某个词」的粗暴匹配 ——
    ///   探针里有一条本机文件路径 F:/haowancheng/...，那不是远程主机，
    ///   按词匹配会误伤（拆成 lua/oo/*.lua 后这个坑暴露出来了）。
    #[test]
    fn script_has_no_hardcoded_remote_host() {
        assert!(!BOOTSTRAP_LUA.contains("52luansha"), "must not carry over their server");
        for scheme in ["http://", "https://"] {
            let mut rest = BOOTSTRAP_LUA;
            while let Some(i) = rest.find(scheme) {
                let tail = &rest[i + scheme.len()..];
                // 只按主机名字符集取，遇到 Lua 模式里的括号等就自然截断，
                // 避免把 "http://([^:/]+)" 这种 Lua 模式误判成远程主机。
                let host: String = tail
                    .chars()
                    .take_while(|c| c.is_ascii_alphanumeric()
                        || matches!(c, '.' | ':' | '-' | '_'))
                    .collect();
                if !host.is_empty() {
                    assert!(
                        host.starts_with("127.0.0.1") || host.starts_with("localhost"),
                        "hardcoded remote host: {host}"
                    );
                }
                rest = tail;
            }
        }
    }

    /// 拆分后各片段必须都在（防止 build.rs 漏拼一个文件）。
    #[test]
    fn script_has_all_fragments() {
        for needle in [
            "-- ===== 01_util.lua =====",
            "-- ===== 02a_protocol.lua =====",
            "-- ===== 02b_protocol.lua =====",
            "-- ===== 03_command.lua =====",
            "-- ===== 04_login.lua =====",
            "-- ===== 05a_probe.lua =====",
            "-- ===== 05b_probe.lua =====",
            "-- ===== 05c_probe.lua =====",
            "-- ===== 06a_hooks.lua =====",
            "-- ===== 06b_hooks.lua =====",
            "OO_Internal_Require",
        ] {
            assert!(BOOTSTRAP_LUA.contains(needle), "fragment missing: {needle}");
        }
    }
}
