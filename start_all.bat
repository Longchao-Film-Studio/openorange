@echo off
start "OO-Web" /min F:\haowancheng\OpenOrange\start_web.bat
timeout /t 1 /nobreak >nul
start "OO-Game" /min F:\haowancheng\OpenOrange\start_game.bat
echo 两个服务已启动（最小化窗口），日志在 work\logs\last.*.log
timeout /t 3 >nul