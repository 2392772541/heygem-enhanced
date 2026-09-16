@echo off
setlocal
chcp 65001 >nul
title HeyGem - 恢复排队调度

set "PAUSE_FILE=%APPDATA%\Duix.Avatar\heygem_queue_runner.paused"

echo.
echo ==================================================
if exist "%PAUSE_FILE%" (
  del /q "%PAUSE_FILE%" >nul 2>&1
  echo [成功] 已清除暂停标记，HeyGem 队列调度恢复！
) else (
  echo [提示] HeyGem 队列未处于暂停状态。
)
echo 等待中的任务将继续被调度器按顺序自动处理。
echo ==================================================
echo.
pause
exit /b 0
