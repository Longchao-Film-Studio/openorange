@echo off
chcp 65001 >nul
rem stop_all.bat 已改由 stop_all.ps1 实现（PowerShell 处理中文与对象输出更可靠）。
rem 这里只做转发，保留旧文件名，避免已有的快捷方式/习惯失效。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1" %*
exit /b %errorlevel%