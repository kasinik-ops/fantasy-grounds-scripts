@echo off
rem Double-click launcher for Windows. All the real work is in run_windows.ps1.
rem ExecutionPolicy Bypass applies to this one run only and changes no system setting;
rem without it, Windows blocks unsigned .ps1 files by default.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_windows.ps1" -Tool find
echo.
pause
