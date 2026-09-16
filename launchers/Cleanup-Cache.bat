@echo off
setlocal
chcp 65001 >nul
title HeyGem - 清理临时缓存

set "PYTHON_EXE=python"
where %PYTHON_EXE% >nul 2>nul
if not "%ERRORLEVEL%"=="0" (
  if exist "C:\Program Files\python\python.exe" set "PYTHON_EXE=C:\Program Files\python\python.exe"
)

echo ==================================================
echo HeyGem 临时缓存与孤儿文件安全清理
echo ==================================================
"%PYTHON_EXE%" "%~dp0..\scripts\cleanup_heygem_cache.py"
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
  echo [错误] 清理失败，代码: %exit_code%
)
pause
exit /b %exit_code%
