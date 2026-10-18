//! 暴露给 Lua 的原生函数（对应闭源方案的 PC_Internal_* 契约）。
//!
//! 这些函数由 lua_pushcclosure 注册进 _G，签名必须是 lua_CFunction：
//!     unsafe extern "C" fn(L: lua_State) -> i32

use crate::bridge;
use crate::capi::{self, Api, LuaState};
use crate::log;
use std::sync::OnceLock;

/// 启动器投递进来的原始 JSON（首次读到即缓存）。
static RESULT_CACHE: OnceLock<String> = OnceLock::new();

/// 取回登录结果。每次调用都尝试从共享内存读；读到即缓存。
pub fn result_json() -> String {
    if let Some(s) = RESULT_CACHE.get() {
        return s.clone();
    }
    let s = bridge::read_result();
    if !s.is_empty() {
        log::write(&format!("bridge: consumed login result ({} bytes)", s.len()));
        let _ = RESULT_CACHE.set(s.clone());
    }
    s
}

/// 结果是否已经就绪。
pub fn result_ready() -> bool {
    !result_json().is_empty()
}

// ---------------- lua_CFunction 实现 ----------------

/// 把 Rust 字符串压栈（唯一需要 Api 的地方，惰性解析一次）。
unsafe fn push_str(l: LuaState, s: &str) -> i32 {
    match Api::load() {
        Ok(api) => {
            let c = std::ffi::CString::new(s).unwrap_or_default();
            (api.pushstring)(l, c.as_ptr() as *const i8);
            1
        }
        Err(e) => {
            log::write(&format!("push_str: Api::load failed: {e}"));
            0
        }
    }
}

/// `OO_Internal_Log(msg)` —— 把 Lua 传来的字符串写进日志。
pub unsafe extern "C" fn l_log(l: LuaState) -> i32 {
    if let Ok(api) = Api::load() {
        let s = capi::to_string(&api, l, 1);
        if !s.is_empty() {
            log::write(&format!("lua: {s}"));
        }
    }
    0
}

/// `OO_Internal_RequestLogin()` -> string
///
/// 返回值语义（与闭源实现一致）：
///   "floating" —— 结果尚未就绪，脚本应挂到 GameTick 轮询；
///   "ready"    —— 结果已就绪，脚本应紧接着调 OO_Internal_Result()。
pub unsafe extern "C" fn l_request_login(l: LuaState) -> i32 {
    let r = result_json();
    if r.is_empty() {
        log::write("login: requested, result not ready -> floating");
        push_str(l, "floating")
    } else {
        log::write(&format!("login: requested, result ready ({} bytes)", r.len()));
        push_str(l, "ready")
    }
}

/// `OO_Internal_Result()` -> string（未就绪时返回空串）
pub unsafe extern "C" fn l_result(l: LuaState) -> i32 {
    let r = result_json();
    push_str(l, &r)
}

/// `OO_Internal_TrackingInfo()` -> JSON 字符串
/// 替代官方 getTrackingInfo，渠道信息由启动器决定，且不外发埋点。
pub unsafe extern "C" fn l_tracking_info(l: LuaState) -> i32 {
    let info = TRACKING_JSON.get_or_init(|| {
        serde_json::json!({
            "platform": "windows",
            "channel_id": "internal_pc",
        })
        .to_string()
    });
    push_str(l, info)
}

/// 渠道信息缓存（实际内容由 DLL 从启动器投递的配置读取，这里给默认值）。
static TRACKING_JSON: OnceLock<String> = OnceLock::new();
