@echo off
setlocal
chcp 65001 >nul
title HeyGem - 停止服务

echo ==================================================
echo         HeyGem 安全停止服务
echo ==================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\stop-heygem.ps1"
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
  echo [错误] 停止异常退出，代码: %exit_code%
  pause
) else (
  echo [成功] HeyGem 相关服务已全部优雅停止！
)
exit /b %exit_code%
