@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
set "folder_csv_exit=%errorlevel%"
if not "%folder_csv_exit%"=="0" pause
exit /b %folder_csv_exit%
