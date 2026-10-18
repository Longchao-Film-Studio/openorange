//! 构建脚本：把 lua/oo_bootstrap.lua 复制进 OUT_DIR，供 include_str! 嵌入 DLL。
//! 这样 DLL 单文件即可工作，但开发期仍可直接编辑 lua 源文件。

use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let lua = manifest.join("../../lua/oo_bootstrap.lua");
    println!("cargo:rerun-if-changed={}", lua.display());
    println!("cargo:rerun-if-changed=build.rs");
    if !lua.is_file() {
        panic!("missing lua script: {}", lua.display());
    }
}
