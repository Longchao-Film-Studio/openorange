#!/usr/bin/env python3
"""解密客户端那几个加密清单（AES-256-ECB + Base64）。

哪些文件是加密的
----------------
    StreamingAssets/assets_md5_cache.json      资源文件 -> md5
    StreamingAssets/version.json               版本信息
    StreamingAssets/package_info.json          包信息
    PersistentPath/HybridData/patch_info.json  补丁信息（patchlist / dlclist 文件名）
    PersistentPath/Patch/patch_md5_cache.json  已下载补丁 -> {md5,size}

密钥从哪来（有实机证据）
------------------------
patch/patchhelper.lua:305-318：

    function PatchHelper.decodeJsonFileByAes(filePath)
      local encodedStr = PatchHelper.readFileContentNoExc(filePath)
      local decodeRet = CSHelper.AesDecryptorBase64(encodedStr, CommonConfig.AesKey)
      ...
    end

CommonConfig.AesKey 是 C# 静态字段。引导脚本里读出来是

    ae125eopp4_54eeff4mmferfkny6oxv0        （32 字节 -> AES-256）

实测模式是 **ECB**（CBC 试过三种 IV 都不对），明文是 UTF-8 JSON。

注意
----
ab_mapping.bin / big_pack_info.bin **不是**加密的，直接用
tools/parse_ab_mapping.py 读，不要走这里。

用法:
    python tools/decrypt_manifest.py --file <加密文件> --out <输出 json>
    python tools/decrypt_manifest.py --all
"""
import argparse
import base64
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    raise SystemExit("需要 pycryptodome: python -m pip install pycryptodome")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GAME = os.environ.get("OO_GAME_DIR") or os.path.join(
    ROOT, "..", "GuiLongchao", "game", "GuiLongchao_Data")

# 实机从 CS.UnityEngine.Utility.Funplus.Runtime.CommonConfig.AesKey 读出来的
AES_KEY = b"ae125eopp4_54eeff4mmferfkny6oxv0"

TARGETS = [
    ("assets_md5_cache", os.path.join(GAME, "StreamingAssets", "assets_md5_cache.json")),
    ("version", os.path.join(GAME, "StreamingAssets", "version.json")),
    ("package_info", os.path.join(GAME, "StreamingAssets", "package_info.json")),
    ("patch_info", os.path.join(GAME, "PersistentPath", "HybridData", "patch_info.json")),
    ("patch_md5_cache", os.path.join(GAME, "PersistentPath", "Patch", "patch_md5_cache.json")),
]


def decrypt_bytes(raw):
    ct = base64.b64decode(raw)
    return unpad(AES.new(AES_KEY, AES.MODE_ECB).decrypt(ct), 16)


def decrypt_file(path):
    """返回 (明文 json 对象, 明文字节数)。"""
    pt = decrypt_bytes(open(path, "rb").read())
    return json.loads(pt.decode("utf-8-sig")), len(pt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--out")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if a.file:
        obj, n = decrypt_file(a.file)
        txt = json.dumps(obj, ensure_ascii=False, indent=1)
        if a.out:
            open(a.out, "w", encoding="utf-8").write(txt)
            print(f'已写出 {a.out}（明文 {n} 字节）')
        else:
            print(txt[:4000])
        return 0

    if not a.all:
        ap.print_help()
        return 1

    outdir = os.path.join(ROOT, "work", "manifests")
    os.makedirs(outdir, exist_ok=True)
    for tag, path in TARGETS:
        if not os.path.exists(path):
            print(f'跳过 {tag:<18} 不存在: {path}')
            continue
        try:
            obj, n = decrypt_file(path)
        except Exception as e:
            print(f'失败 {tag:<18} {e}')
            continue
        out = os.path.join(outdir, tag + ".json")
        open(out, "w", encoding="utf-8").write(
            json.dumps(obj, ensure_ascii=False, indent=1))
        print(f'OK   {tag:<18} 明文 {n:8d} 字节 -> {out}')
        if tag == "patch_info":
            info = obj.get("patchinfo", {})
            rel = info.get("release", {}).get("win64", {}).get("release", {})
            for k in ("patchlist", "dlclist", "hybridlist", "target_full_ver"):
                if k in rel:
                    print(f"       {k} = {rel[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())