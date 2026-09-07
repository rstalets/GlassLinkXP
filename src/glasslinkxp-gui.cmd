@echo off
rem ---------------------------------------------------------------------------
rem  Open the G1000 softkey window.
rem
rem  Double-click this file, or make a shortcut to it. Like glasslinkxp.cmd it calls
rem  the venv interpreter directly, so there is nothing to activate and
rem  PowerShell's execution policy never enters into it.
rem
rem  It launches pythonw.exe where there is one, so the window comes up on its
rem  own instead of with a console behind it -- but it checks for Tk with the
rem  console interpreter first, because a pythonw that cannot import tkinter
rem  fails with no window and no message at all.
rem ---------------------------------------------------------------------------
setlocal
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"

if not exist "%PY%" (
    echo(
    echo   No interpreter at %PY%
    echo(
    echo   Run install.cmd in the folder above this one first.
    echo(
    exit /b 1
)

"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo(
    echo   This Python has no Tk support, so the window cannot be opened.
    echo(
    echo       %PY%
    echo(
    echo   Everything the window does can be done from the command line, which
    echo   needs none of it:
    echo       glasslinkxp --help
    echo(
    exit /b 1
)

if exist "%PYW%" (
    start "" "%PYW%" -m glasslinkxp.gui %*
) else (
    "%PY%" -m glasslinkxp.gui %*
)
