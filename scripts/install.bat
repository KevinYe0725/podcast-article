@echo off
REM podcast-article one-click installer for Windows (double-click this file).
REM It calls install.ps1 next to it. Pass arguments from a command prompt, e.g.
REM   install.bat -Dir D:\pa -Check
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
