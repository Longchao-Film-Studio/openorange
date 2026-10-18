@echo off
chcp 65001 >nul
REM OpenOrange 一键启动游戏(带代理) - 双击本文件即可
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0play_with_proxy.ps1"
pause
