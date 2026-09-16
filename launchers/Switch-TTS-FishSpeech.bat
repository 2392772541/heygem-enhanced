@echo off
setlocal
chcp 65001 >nul
title HeyGem - 切回原版 Fish-Speech 模式

echo 正在切换语音合成为原版 Docker Fish-Speech 模式...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\switch-heygem-tts.ps1" -Mode "fish-speech"
set "exit_code=%ERRORLEVEL%"

echo.
if "%exit_code%"=="0" (
  echo [成功] 已设置模式为 fish-speech (如果服务正在运行，将在下次生成或重启后生效)
) else (
  echo [错误] 切换失败，代码: %exit_code%
)
pause
exit /b %exit_code%
