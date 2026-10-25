#!/usr/bin/env python3
"""web.assets —— 静态资源与页面模板加载（P0-E3 从 web/app.py 抽出）。

约定：
  web/static/      CSS / JS（可按 /static/<name> 直接下发）
  web/templates/   页面 HTML 片段（只由服务端拼进页面，不单独下发）

★ 页面片段与样式放在文件里，是为了让 app.py 只剩「路由 + 业务」。
  以前 900 行 CSS 和 400 行 JS 全内联在 app.py，改一行样式要翻半天。
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_HERE, "static")
TEMPLATE_DIR = os.path.join(_HERE, "templates")

# 允许从 /static/ 下发的后缀 -> Content-Type
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def read_static(name):
    """读 web/static 下的文本文件（CSS / JS），按 UTF-8。"""
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        return f.read()


def read_template(name):
    """读 web/templates 下的 HTML 片段，按 UTF-8。"""
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


# 账号中心
CSS = read_static("app.css")
SCRIPT = read_static("app.js")
BRAND = read_template("brand.html")
BODY = read_template("index_body.html")
OVERLAY = read_template("overlay.html")

# 管理控制台
ADMIN_CSS = read_static("admin.css")
ADMIN_SCRIPT = read_static("admin.js")
ADMIN_BODY = read_template("admin_body.html")

# 公告 / 喊话页面（2026-10-26）
ANNOUNCE_SCRIPT = read_static("announce.js")
ANNOUNCE_BODY = read_template("announce_body.html")
