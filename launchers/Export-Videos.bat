@echo off
setlocal
chcp 65001 >nul
title HeyGem - 批量导出成片

echo ==================================================
echo 正在扫描 HeyGem 数据库并自动交付已完成成片...
echo ==================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\export-heygem-videos.ps1"
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
  echo [错误] 导出失败，请查看日志: %~dp0..\logs\export-heygem-videos.log
) else (
  echo [成功] 导出与同步完成！
)
pause
exit /b %exit_code%
