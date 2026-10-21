//! 构建脚本：把 lua/oo/*.lua 按文件名顺序拼成一个 Lua chunk，写进 OUT_DIR，
//! 供 include_str! 嵌入 DLL。
//!
//! 为什么要拼成一个 chunk 而不是 require：
//!   这些片段跑在**游戏自己的 Lua 虚拟机**里，路径不在 package.path 上，require 找不到；
//!   而且它们要共享同一批 local 变量。拼成同一个 chunk 与拆分前语义完全一致。
//!   开发期仍然可以直接编辑 lua/oo/ 下的源文件。

use std::fs;
use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let lua_dir = manifest.join("../../lua/oo");
    let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap());

    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={}", lua_dir.display());

    let mut files: Vec<PathBuf> = Vec::new();
    if lua_dir.is_dir() {
        for ent in fs::read_dir(&lua_dir).expect("read lua/oo") {
            let p = ent.expect("dir entry").path();
            if p.extension().and_then(|s| s.to_str()) == Some("lua") {
                files.push(p);
            }
        }
    }
    files.sort();
    if files.is_empty() {
        panic!("missing lua fragments under {}", lua_dir.display());
    }

    let mut buf = String::new();
    for p in &files {
        println!("cargo:rerun-if-changed={}", p.display());
        buf.push_str(&format!(
            "\n-- ===== {} =====\n",
            p.file_name().unwrap().to_string_lossy()
        ));
        buf.push_str(&fs::read_to_string(p).expect("read lua fragment"));
        if !buf.ends_with('\n') {
            buf.push('\n');
        }
    }

    let dest = out_dir.join("oo_bootstrap.generated.lua");
    fs::write(&dest, buf).expect("write generated lua");
}
