//! 内嵌的引导 Lua 脚本。
//!
//! 源文件位于 launcher/lua/oo_bootstrap.lua，构建时通过 include_str! 编译进 DLL，
//! 因此发布时只需 OO_Launcher.exe + OO_Internal.dll + OpenOrange.ini 三个文件。

/// 引导脚本全文。
pub const BOOTSTRAP_LUA: &str = include_str!("../../../lua/oo_bootstrap.lua");

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
        assert!(BOOTSTRAP_LUA.len() > 1000, "script suspiciously short: {}", BOOTSTRAP_LUA.len());
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

    #[test]
    fn script_has_no_hardcoded_remote_host() {
        assert!(!BOOTSTRAP_LUA.contains("52luansha"), "must not carry over their server");
        assert!(!BOOTSTRAP_LUA.contains("haowancheng"), "must not carry over vendor hosts");
    }
}
