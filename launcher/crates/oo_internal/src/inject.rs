//! 注入动作：把 OO_Internal_* 原生函数与引导脚本送入 Lua 状态机。

use crate::capi::{self, Api, LuaCFunction, LuaState};
use crate::log;
use crate::native;
use crate::script;

/// 是否已完成注入。
static DONE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub fn is_done() -> bool {
    DONE.load(std::sync::atomic::Ordering::SeqCst)
}

/// 注册一个 C 函数为全局名。
unsafe fn register(api: &Api, l: LuaState, name: &str, f: LuaCFunction) -> Result<(), String> {
    let cname = std::ffi::CString::new(name).map_err(|e| format!("bad name {name}: {e}"))?;
    let before = (api.gettop)(l);
    (api.pushcclosure)(l, f, 0);
    let after_push = (api.gettop)(l);
    (api.setglobal)(l, cname.as_ptr() as *const i8);
    let after_set = (api.gettop)(l);
    log::write(&format!(
        "inject: register {name}: top {before}->{after_push}->{after_set}"
    ));
    if after_push != before + 1 {
        return Err(format!(
            "lua_pushcclosure did not push for {name}: {before} -> {after_push}"
        ));
    }
    if after_set != before {
        return Err(format!(
            "stack imbalance registering {name}: {before} -> {after_set}"
        ));
    }
    Ok(())
}

/// 执行完整注入流程。
pub unsafe fn run(l: LuaState) -> Result<(), String> {
    // 先置位再干活，防止任何路径上的重入。
    if DONE.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return Ok(());
    }

    log::write("inject: run() entered");
    let api = Api::load()?;
    log::write("inject: lua C API resolved (gettop/settop/pushcclosure/setglobal/...)");

    let top_before = (api.gettop)(l);
    log::write(&format!("inject: lua stack top at entry = {top_before}"));

    // ---- 1) 注册原生函数 ----
    register(&api, l, "OO_Internal_Log", native::l_log)?;
    register(&api, l, "OO_Internal_RequestLogin", native::l_request_login)?;
    register(&api, l, "OO_Internal_Result", native::l_result)?;
    register(&api, l, "OO_Internal_TrackingInfo", native::l_tracking_info)?;
    log::write(&format!(
        "inject: 4 native functions registered, top = {}",
        (api.gettop)(l)
    ));

    // ---- 1.5) 把启动参数送进 Lua ----
    // --auto 才启用「自动进入游戏」；默认 false。
    // 由启动器通过环境变量 OO_AUTO 传递（游戏进程继承父进程环境）。
    let auto = std::env::var("OO_AUTO").map(|v| v == "1").unwrap_or(false);
    let prelude = format!("rawset(_G, 'OO_AUTO', {})", if auto { "true" } else { "false" });
    log::write(&format!("inject: OO_AUTO = {auto}"));
    capi::load_chunk(l, &prelude, "@oo_prelude")?;
    capi::pcall(&api, l, 0, 0)?;

    // ---- 2) 载入引导脚本 ----
    log::write(&format!(
        "inject: about to load bootstrap chunk ({} bytes) via trampoline",
        script::BOOTSTRAP_LUA.len()
    ));
    log::write(&format!("inject: trampoline = {:p}", crate::xlua::trampoline()));
    capi::load_chunk(l, script::BOOTSTRAP_LUA, "@oo_bootstrap.lua")?;
    log::write(&format!(
        "inject: bootstrap chunk compiled, top = {}",
        (api.gettop)(l)
    ));

    capi::pcall(&api, l, 0, 0)?;
    log::write("inject: bootstrap script executed");

    // ---- 3) 校验脚本确实生效 ----
    let check = r#"
return tostring(rawget(_G, 'OO_Internal_Ready') == true)
        .. '|' .. tostring(type(rawget(_G, 'OO_Internal_RequestLogin')))
        .. '|' .. tostring(type(rawget(_G, 'require')))
"#;
    capi::load_chunk(l, check, "@oo_verify")?;
    capi::pcall(&api, l, 0, 1)?;
    let report = capi::to_string(&api, l, -1);
    (api.settop)(l, top_before);
    log::write(&format!("inject: verify -> {report}"));

    if !report.starts_with("true|function|function") {
        return Err(format!("bootstrap did not take effect: {report}"));
    }

    log::write("inject: DONE");
    Ok(())
}
