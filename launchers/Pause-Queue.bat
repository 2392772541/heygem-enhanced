@echo off
setlocal
chcp 65001 >nul
title HeyGem - 暂停排队调度

set "PAUSE_DIR=%APPDATA%\Duix.Avatar"
set "PAUSE_FILE=%PAUSE_DIR%\heygem_queue_runner.paused"

if not exist "%PAUSE_DIR%" mkdir "%PAUSE_DIR%" >nul 2>&1

> "%PAUSE_FILE%" (
  echo paused_at=%date% %time%
)

echo.
echo ==================================================
echo [成功] HeyGem 队列已暂停！
echo 调度器将不会向后端分发新的等待任务。
echo (当前已经在渲染的视频任务仍将继续完成)
echo ==================================================
echo.
pause
exit /b 0
