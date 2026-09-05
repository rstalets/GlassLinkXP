@echo off
rem ---------------------------------------------------------------------------
rem  Run the G1000 softkey daemon without activating anything.
rem
rem  A .cmd file is not subject to PowerShell's execution policy, so this works
rem  from PowerShell, cmd, a shortcut or Task Scheduler with no Set-ExecutionPolicy
rem  and no Activate.ps1. It simply calls the venv interpreter directly, which is
rem  all `activate` really arranges for.
rem
rem    g1000 list-windows
rem    g1000 calibrate --display pfd
rem    g1000 run
rem
rem  For the window over all of it, use g1000-gui.cmd (or `g1000 gui`).
rem ---------------------------------------------------------------------------
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo(
    echo   No interpreter at %PY%
    echo(
    echo   The virtual environment has not been created yet. Run:
    echo       powershell -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1"
    echo(
    exit /b 1
)
"%PY%" -m g1000_softkey.main %*
