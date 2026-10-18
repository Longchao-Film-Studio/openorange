//! OpenOrange.ini 读取与解析。
//!
//! 设计约束（用户明确要求）：服务器地址不编译进程序，
//! 全部从启动器同目录的外部 ini 读取，改 ini 重启即生效。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub login_url: String,
    pub serverlist_url: String,
    pub game_host: String,
    pub game_port: u16,
    /// ini 里显式写的显示名。留空表示"启动时向 info_url 查询"。
    pub server_name: String,
    /// 服务器信息接口（用于拉取显示名）。留空则跳过查询。
    pub info_url: String,
    pub timeout_ms: u64,
    pub verify_sign: bool,
}

#[derive(Debug, Clone)]
pub struct GameConfig {
    pub game_exe: String,
    pub inject_dll: String,
    pub game_args: String,
    pub keep_alive: bool,
}

#[derive(Debug, Clone)]
pub struct AccountConfig {
    pub remember: bool,
    pub auto_login: bool,
}

#[derive(Debug, Clone)]
pub struct ChannelConfig {
    pub channel_id: String,
    pub platform: String,
}

#[derive(Debug, Clone)]
pub struct DebugConfig {
    pub log_level: String,
    pub log_file: bool,
    pub console: bool,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub server: ServerConfig,
    pub game: GameConfig,
    pub account: AccountConfig,
    pub channel: ChannelConfig,
    pub debug: DebugConfig,
    /// ini 文件实际所在的绝对路径
    pub ini_path: PathBuf,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            server: ServerConfig {
                login_url: "http://127.0.0.1:8322/api/launcher/login".into(),
                serverlist_url: "http://127.0.0.1:8080/client/serverlist/v1".into(),
                game_host: "127.0.0.1".into(),
                game_port: 33100,
                // 注意：这里必须是空串，不能用 "OpenOrange"。
                // get() 对空值返回 None，因此 ini 里写 server_name =（留空）不会覆盖此项；
                // 若默认值非空，configured_display_name() 就会误判为"ini 已固定"而跳过网络查询。
                // 默认名的兜底交给 Config::DEFAULT_DISPLAY_NAME / display_name_now()。
                server_name: String::new(),
                info_url: "http://127.0.0.1:8322/api/server/info".into(),
                timeout_ms: 8000,
                verify_sign: false,
            },
            game: GameConfig {
                game_exe: String::new(),
                inject_dll: "OO_Internal.dll".into(),
                game_args: String::new(),
                keep_alive: true,
            },
            account: AccountConfig { remember: true, auto_login: false },
            channel: ChannelConfig {
                channel_id: "internal_pc".into(),
                platform: "windows".into(),
            },
            debug: DebugConfig { log_level: "info".into(), log_file: true, console: false },
            ini_path: PathBuf::new(),
        }
    }
}

/// 解析 ini 文本为 "节.键" -> 值 的扁平映射（键统一小写）。
pub fn parse_ini(text: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let mut section = String::from("");
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() { continue; }
        if line.starts_with(';') || line.starts_with('#') { continue; }
        if line.starts_with('[') && line.ends_with(']') {
            section = line[1..line.len() - 1].trim().to_ascii_lowercase();
            continue;
        }
        if let Some(eq) = line.find('=') {
            let k = line[..eq].trim().to_ascii_lowercase();
            let v = line[eq + 1..].trim().to_string();
            let full = if section.is_empty() { k } else { format!("{section}.{k}") };
            out.insert(full, v);
        }
    }
    out
}

fn get(m: &HashMap<String, String>, key: &str) -> Option<String> {
    m.get(key).map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn get_bool(m: &HashMap<String, String>, key: &str, dflt: bool) -> bool {
    match get(m, key) {
        Some(v) => matches!(v.as_str(), "1" | "true" | "yes" | "on" | "TRUE" | "True"),
        None => dflt,
    }
}

fn get_u64(m: &HashMap<String, String>, key: &str, dflt: u64) -> u64 {
    get(m, key).and_then(|v| v.parse().ok()).unwrap_or(dflt)
}

impl Config {
    /// 从指定 ini 路径加载；文件不存在时返回默认配置并标记。
    pub fn load(path: &Path) -> (Config, bool) {
        let mut cfg = Config::default();
        cfg.ini_path = path.to_path_buf();
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => return (cfg, false),
        };
        // 兼容 UTF-8 BOM
        let text = text.trim_start_matches('\u{feff}');
        let m = parse_ini(text);

        if let Some(v) = get(&m, "server.login_url") { cfg.server.login_url = v; }
        if let Some(v) = get(&m, "server.serverlist_url") { cfg.server.serverlist_url = v; }
        if let Some(v) = get(&m, "server.game_host") { cfg.server.game_host = v; }
        cfg.server.game_port = get_u64(&m, "server.game_port", cfg.server.game_port as u64) as u16;
        // server_name 允许为空 —— 空 = 启动时向 info_url 查询
        if let Some(v) = get(&m, "server.server_name") { cfg.server.server_name = v; }
        if let Some(v) = get(&m, "server.info_url") { cfg.server.info_url = v; }
        cfg.server.timeout_ms = get_u64(&m, "server.timeout_ms", cfg.server.timeout_ms);
        cfg.server.verify_sign = get_bool(&m, "server.verify_sign", cfg.server.verify_sign);

        if let Some(v) = get(&m, "game.game_exe") { cfg.game.game_exe = v; }
        if let Some(v) = get(&m, "game.inject_dll") { cfg.game.inject_dll = v; }
        if let Some(v) = get(&m, "game.game_args") { cfg.game.game_args = v; }
        cfg.game.keep_alive = get_bool(&m, "game.keep_alive", cfg.game.keep_alive);

        cfg.account.remember = get_bool(&m, "account.remember", cfg.account.remember);
        cfg.account.auto_login = get_bool(&m, "account.auto_login", cfg.account.auto_login);

        if let Some(v) = get(&m, "channel.channel_id") { cfg.channel.channel_id = v; }
        if let Some(v) = get(&m, "channel.platform") { cfg.channel.platform = v; }

        if let Some(v) = get(&m, "debug.log_level") { cfg.debug.log_level = v; }
        cfg.debug.log_file = get_bool(&m, "debug.log_file", cfg.debug.log_file);
        cfg.debug.console = get_bool(&m, "debug.console", cfg.debug.console);

        (cfg, true)
    }

    /// 内置兜底显示名。
    pub const DEFAULT_DISPLAY_NAME: &'static str = "OpenOrange";

    /// 返回 ini 中显式配置的显示名；为空则返回 None（表示需要向服务器查询）。
    pub fn configured_display_name(&self) -> Option<&str> {
        let v = self.server.server_name.trim();
        if v.is_empty() { None } else { Some(v) }
    }

    /// 无需网络时的显示名：ini 显式值 -> 内置默认值。
    pub fn display_name_now(&self) -> String {
        self.configured_display_name()
            .unwrap_or(Self::DEFAULT_DISPLAY_NAME)
            .to_string()
    }

    /// 启动器 exe 所在目录。
    pub fn exe_dir() -> PathBuf {
        std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."))
    }

    /// 解析游戏 exe 的绝对路径。
    ///
    /// 规则（刻意保持简单、可预测，不做任何目录搜索）：
    ///   1. ini 的 [Game] game_exe 非空 -> 强制使用它。
    ///      相对路径按“启动器所在目录”解析；文件不存在就直接报错，
    ///      绝不悄悄去别处找 —— 否则用户无法判断到底启动了哪个 exe。
    ///   2. ini 未配置（空）        -> 只看启动器同级目录下的 GuiLongchao.exe。
    pub fn resolve_game_exe(&self) -> Result<PathBuf, String> {
        let dir = Self::exe_dir();
        let raw = self.game.game_exe.trim();

        if !raw.is_empty() {
            let p = PathBuf::from(raw);
            let cand = if p.is_absolute() { p } else { dir.join(p) };
            let shown = normalize_path(&cand);
            if shown.is_file() {
                return Ok(shown);
            }
            return Err(format!(
                "OpenOrange.ini 里配置的游戏路径不存在：\n\n{}\n\n解析为：{}\n\n启动器目录：{}",
                raw,
                shown.display(),
                dir.display()
            ));
        }

        // ini 未配置：只认启动器同级目录
        let cand = dir.join("GuiLongchao.exe");
        if cand.is_file() {
            return Ok(cand);
        }
        Err(format!(
            "OpenOrange.ini 未配置游戏路径，且启动器同级目录下没有 GuiLongchao.exe。\n\n查找位置：{}\n\n请在 ini 的 [Game] game_exe 填写完整路径。",
            cand.display()
        ))
    }

    /// 解析注入 DLL 的绝对路径（固定与启动器同目录）。
    pub fn resolve_inject_dll(&self) -> PathBuf {
        let name = self.game.inject_dll.trim();
        let p = PathBuf::from(name);
        if p.is_absolute() { p } else { Self::exe_dir().join(name) }
    }
}

/// 逐级去掉 . 与 .. 分量，得到可直接展示的规范化路径。
///
/// 不再要求文件存在 —— 调用方需要在“路径不存在”时把规范化结果打进错误信息，
/// 让用户看到自己配的路径到底被解析成了什么。
fn normalize_path(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            std::path::Component::ParentDir => { out.pop(); }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sections_and_values() {
        let text = "[Server]\n; comment\nlogin_url = http://a/b\ngame_port=9999\n[Debug]\nlog_level=debug\n";
        let m = parse_ini(text);
        assert_eq!(m.get("server.login_url").unwrap(), "http://a/b");
        assert_eq!(m.get("server.game_port").unwrap(), "9999");
        assert_eq!(m.get("debug.log_level").unwrap(), "debug");
    }

    #[test]
    fn loads_real_ini_if_present() {
        let p = Path::new("../../dist/OpenOrange.ini");
        if p.exists() {
            let (cfg, ok) = Config::load(p);
            assert!(ok);
            assert!(cfg.server.login_url.starts_with("http"));
            assert!(cfg.server.game_port > 0);
        }
    }

    #[test]
    fn missing_file_falls_back_to_defaults() {
        let (cfg, ok) = Config::load(Path::new("__nope__.ini"));
        assert!(!ok);
        assert_eq!(cfg.server.game_port, 33100);
    }
}
