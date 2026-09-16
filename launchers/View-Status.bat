@echo off
setlocal
chcp 65001 >nul
title HeyGem - 实时运行状态与监控

set "PYTHON_EXE=python"
where %PYTHON_EXE% >nul 2>nul
if not "%ERRORLEVEL%"=="0" (
  if exist "C:\Program Files\python\python.exe" set "PYTHON_EXE=C:\Program Files\python\python.exe"
)

"%PYTHON_EXE%" "%~dp0..\scripts\view_heygem_process_status.py"
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo [提示] 监控进程退出，代码: %exit_code%
  pause
)
exit /b %exit_code%
