@echo off
setlocal
chcp 65001 >nul
title HeyGem - 创建桌面启动工具箱

echo ==================================================
echo   正在为本机生成 HeyGem-常用启动器 桌面快捷方式...
echo ==================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\generate-desktop-shortcuts.ps1"
pause
