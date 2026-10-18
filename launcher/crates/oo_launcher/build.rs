//! 构建脚本：当前不使用外部 crate 嵌入清单。
//! 管理员权限通过 launcher.manifest + 运行时自提升（见 ensure_elevated）实现。

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=../../OpenOrange.ini");
    println!("cargo:rerun-if-changed=../../lua/oo_bootstrap.lua");
}
