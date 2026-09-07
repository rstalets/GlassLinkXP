@echo off
rem ---------------------------------------------------------------------------
rem  GlassLinkXP installer. Double-click this file.
rem
rem  It runs install.ps1, which copies the app into %APPDATA%\GlassLinkXP,
rem  installs its Python environment there, and adds a desktop shortcut.
rem  A .cmd file is not subject to PowerShell's execution policy, so this
rem  works with no Set-ExecutionPolicy needed first.
rem ---------------------------------------------------------------------------
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
echo(
pause
exit /b %RC%
