//! 登录请求。走 WinINet（Windows 系统原生网络栈，不引入任何 web 技术）。
//!
//! 协议：POST <login_url>  body = {"mobile":"...","password":"...", ...}
//! 期望响应：{"code":1,"msg":"Success","data":{account_id,ticket,...}}

use oo_shared::LoginEnvelope;
use std::ffi::c_void;
use windows::core::PCWSTR;
use windows::Win32::Networking::WinInet::{
    HttpOpenRequestW, HttpSendRequestW, InternetCloseHandle, InternetConnectW, InternetOpenW,
    InternetReadFile, InternetSetOptionW, INTERNET_FLAG_KEEP_CONNECTION,
    INTERNET_FLAG_NO_CACHE_WRITE, INTERNET_FLAG_NO_UI, INTERNET_FLAG_RELOAD,
    INTERNET_FLAG_SECURE, INTERNET_OPEN_TYPE_PRECONFIG, INTERNET_OPTION_CONNECT_TIMEOUT,
    INTERNET_OPTION_RECEIVE_TIMEOUT, INTERNET_OPTION_SEND_TIMEOUT, INTERNET_SERVICE_HTTP,
};

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub struct LoginRequest {
    pub url: String,
    pub body: String,
    pub timeout_ms: u64,
}

/// 拆分 URL 为 (host, path, secure, port)。
pub fn split_url(url: &str) -> Result<(String, String, bool, u16), String> {
    let (secure, rest) = if let Some(r) = url.strip_prefix("https://") {
        (true, r)
    } else if let Some(r) = url.strip_prefix("http://") {
        (false, r)
    } else {
        return Err(format!("unsupported url scheme: {url}"));
    };
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match authority.rfind(':') {
        Some(i) => {
            let p: u16 = authority[i + 1..].parse().map_err(|_| format!("bad port in {url}"))?;
            (authority[..i].to_string(), p)
        }
        None => (authority.to_string(), if secure { 443 } else { 80 }),
    };
    if host.is_empty() {
        return Err(format!("empty host in {url}"));
    }
    Ok((host, path.to_string(), secure, port))
}

/// 阻塞执行登录请求。返回解析后的封套。
pub fn post_login(req: &LoginRequest) -> Result<LoginEnvelope, String> {
    let (host, path, secure, port) = split_url(&req.url)?;
    unsafe { post_login_inner(&host, &path, secure, port, req) }
}

unsafe fn post_login_inner(
    host: &str,
    path: &str,
    secure: bool,
    port: u16,
    req: &LoginRequest,
) -> Result<LoginEnvelope, String> {
    let agent = wide("OpenOrange Launcher/0.1");
    let whost = wide(host);
    let wpath = wide(path);

    let session = InternetOpenW(
        PCWSTR(agent.as_ptr()),
        INTERNET_OPEN_TYPE_PRECONFIG.0,
        PCWSTR::null(),
        PCWSTR::null(),
        0,
    );
    if session.is_null() {
        return Err("InternetOpenW failed".into());
    }
    let guard = SessionGuard(session);

    // 超时设置
    let t = req.timeout_ms as u32;
    for opt in [
        INTERNET_OPTION_CONNECT_TIMEOUT,
        INTERNET_OPTION_SEND_TIMEOUT,
        INTERNET_OPTION_RECEIVE_TIMEOUT,
    ] {
        let _ = InternetSetOptionW(
            Some(session),
            opt,
            Some(&t as *const u32 as *const c_void),
            std::mem::size_of::<u32>() as u32,
        );
    }

    let connect = InternetConnectW(
        session,
        PCWSTR(whost.as_ptr()),
        port,
        PCWSTR::null(),
        PCWSTR::null(),
        INTERNET_SERVICE_HTTP,
        0,
        None,
    );
    if connect.is_null() {
        return Err(format!("InternetConnectW failed for {host}:{port}"));
    }
    let _connect_guard = SessionGuard(connect);

    let mut flags =
        INTERNET_FLAG_NO_CACHE_WRITE | INTERNET_FLAG_RELOAD | INTERNET_FLAG_KEEP_CONNECTION | INTERNET_FLAG_NO_UI;
    if secure {
        flags |= INTERNET_FLAG_SECURE;
    }

    let request = HttpOpenRequestW(
        connect,
        windows::core::w!("POST"),
        PCWSTR(wpath.as_ptr()),
        PCWSTR::null(),
        PCWSTR::null(),
        None,
        flags,
        None,
    );
    if request.is_null() {
        return Err("HttpOpenRequestW failed".into());
    }
    let _request_guard = SessionGuard(request);

    let headers = wide("Content-Type: application/json; charset=utf-8\r\nAccept: application/json\r\n");
    // 去掉结尾 NUL，HttpSendRequestW 接收切片
    let headers_slice = &headers[..headers.len() - 1];
    let body = req.body.as_bytes();

    HttpSendRequestW(
        request,
        Some(headers_slice),
        Some(body.as_ptr() as *const c_void),
        body.len() as u32,
    )
    .map_err(|e| format!("HttpSendRequestW failed: {e}"))?;

    let mut buf: Vec<u8> = Vec::with_capacity(8192);
    let mut chunk = [0u8; 4096];
    loop {
        let mut read = 0u32;
        InternetReadFile(
            request,
            chunk.as_mut_ptr() as *mut c_void,
            chunk.len() as u32,
            &mut read,
        )
        .map_err(|e| format!("InternetReadFile failed: {e}"))?;
        if read == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..read as usize]);
        if buf.len() > 4 * 1024 * 1024 {
            return Err("response too large".into());
        }
    }
    drop(_request_guard);
    drop(_connect_guard);
    drop(guard);

    let text = String::from_utf8_lossy(&buf).to_string();
    let text = text.trim();
    if text.is_empty() {
        return Err("empty response from server".into());
    }
    parse_envelope(text)
}

/// GET 请求，返回响应体文本。用于拉取服务器公开信息（如显示名）。
pub fn get_text(url: &str, timeout_ms: u64) -> Result<String, String> {
    let (host, path, secure, port) = split_url(url)?;
    unsafe { get_inner(&host, &path, secure, port, timeout_ms) }
}

unsafe fn get_inner(
    host: &str,
    path: &str,
    secure: bool,
    port: u16,
    timeout_ms: u64,
) -> Result<String, String> {
    let agent = wide("OpenOrange Launcher/0.1");
    let whost = wide(host);
    let wpath = wide(path);

    let session = InternetOpenW(
        PCWSTR(agent.as_ptr()),
        INTERNET_OPEN_TYPE_PRECONFIG.0,
        PCWSTR::null(),
        PCWSTR::null(),
        0,
    );
    if session.is_null() {
        return Err("InternetOpenW failed".into());
    }
    let guard = SessionGuard(session);

    let t = timeout_ms as u32;
    for opt in [
        INTERNET_OPTION_CONNECT_TIMEOUT,
        INTERNET_OPTION_SEND_TIMEOUT,
        INTERNET_OPTION_RECEIVE_TIMEOUT,
    ] {
        let _ = InternetSetOptionW(
            Some(session),
            opt,
            Some(&t as *const u32 as *const c_void),
            std::mem::size_of::<u32>() as u32,
        );
    }

    let connect = InternetConnectW(
        session,
        PCWSTR(whost.as_ptr()),
        port,
        PCWSTR::null(),
        PCWSTR::null(),
        INTERNET_SERVICE_HTTP,
        0,
        None,
    );
    if connect.is_null() {
        return Err(format!("InternetConnectW failed for {host}:{port}"));
    }
    let _connect_guard = SessionGuard(connect);

    let mut flags =
        INTERNET_FLAG_NO_CACHE_WRITE | INTERNET_FLAG_RELOAD | INTERNET_FLAG_KEEP_CONNECTION | INTERNET_FLAG_NO_UI;
    if secure {
        flags |= INTERNET_FLAG_SECURE;
    }

    let request = HttpOpenRequestW(
        connect,
        windows::core::w!("GET"),
        PCWSTR(wpath.as_ptr()),
        PCWSTR::null(),
        PCWSTR::null(),
        None,
        flags,
        None,
    );
    if request.is_null() {
        return Err("HttpOpenRequestW failed".into());
    }
    let _request_guard = SessionGuard(request);

    let headers = wide("Accept: application/json\r\n");
    let headers_slice = &headers[..headers.len() - 1];
    HttpSendRequestW(request, Some(headers_slice), None, 0)
        .map_err(|e| format!("HttpSendRequestW(GET) failed: {e}"))?;

    let mut buf: Vec<u8> = Vec::with_capacity(8192);
    let mut chunk = [0u8; 4096];
    loop {
        let mut read = 0u32;
        InternetReadFile(
            request,
            chunk.as_mut_ptr() as *mut c_void,
            chunk.len() as u32,
            &mut read,
        )
        .map_err(|e| format!("InternetReadFile failed: {e}"))?;
        if read == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..read as usize]);
        if buf.len() > 1024 * 1024 {
            return Err("response too large".into());
        }
    }
    Ok(String::from_utf8_lossy(&buf).to_string())
}

/// 查询服务器显示名。任何失败都返回 Err，调用方应退回默认名。
pub fn fetch_server_name(url: &str, timeout_ms: u64) -> Result<String, String> {
    let text = get_text(url, timeout_ms)?;
    let v: serde_json::Value =
        serde_json::from_str(text.trim()).map_err(|e| format!("invalid json: {e}"))?;
    // 支持两种形态：{code,data:{server_name}} 或直接 {server_name}
    let name = v
        .get("data")
        .and_then(|d| d.get("server_name"))
        .or_else(|| v.get("server_name"))
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if name.is_empty() {
        return Err(format!("server_name missing in response: {text}"));
    }
    Ok(name)
}

/// 句柄 RAII 守卫。
struct SessionGuard(*const c_void);
impl Drop for SessionGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = InternetCloseHandle(self.0);
        }
    }
}

/// 把服务端响应文本解析成封套。容忍两种形态：
///   1) {"code":1,"msg":...,"data":{...}}  —— 我们的标准格式
///   2) {"data":{"code":1,...}}            —— 外层再包一层
pub fn parse_envelope(text: &str) -> Result<LoginEnvelope, String> {
    let v: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("invalid json: {e}; body={text}"))?;

    if v.get("code").is_some() {
        return serde_json::from_value(v).map_err(|e| format!("bad envelope: {e}"));
    }
    if let Some(inner) = v.get("data") {
        if inner.get("code").is_some() {
            return serde_json::from_value(inner.clone())
                .map_err(|e| format!("bad inner envelope: {e}"));
        }
    }
    Err(format!("unrecognized response shape: {text}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_urls() {
        assert_eq!(
            split_url("http://127.0.0.1:8322/a/b").unwrap(),
            ("127.0.0.1".into(), "/a/b".into(), false, 8322)
        );
        assert_eq!(split_url("https://x.cn/login").unwrap(), ("x.cn".into(), "/login".into(), true, 443));
        assert!(split_url("ftp://x").is_err());
    }

    #[test]
    fn parses_standard_envelope() {
        let e = parse_envelope(r#"{"code":1,"msg":"Success","data":{"account_id":"a1"}}"#).unwrap();
        assert!(e.is_ok());
        assert_eq!(e.data["account_id"], "a1");
    }

    #[test]
    fn parses_nested_envelope() {
        let e = parse_envelope(r#"{"data":{"code":1,"msg":"ok","data":{"account_id":"a2"}}}"#).unwrap();
        assert_eq!(e.data["account_id"], "a2");
    }

    #[test]
    fn rejects_garbage() {
        assert!(parse_envelope("not json").is_err());
        assert!(parse_envelope(r#"{"x":1}"#).is_err());
    }
}
