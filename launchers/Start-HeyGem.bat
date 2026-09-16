@echo off
setlocal
chcp 65001 >nul
title HeyGem - 启动服务

echo ==================================================
echo         HeyGem 启动服务 (Docker + 队列调度)
echo ==================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\start-heygem.ps1"
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
  echo [错误] 启动异常退出，代码: %exit_code%
  echo 详细排查日志请查看: %~dp0..\logs\start-heygem.log
  pause
) else (
  echo [成功] HeyGem 相关服务已就绪！
)
exit /b %exit_code%
