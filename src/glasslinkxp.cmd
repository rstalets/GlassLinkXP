@echo off
rem ---------------------------------------------------------------------------
rem  Run the G1000 softkey daemon without activating anything.
rem
rem  A .cmd file is not subject to PowerShell's execution policy, so this works
rem  from PowerShell, cmd, a shortcut or Task Scheduler with no Set-ExecutionPolicy
rem  and no Activate.ps1. It simply calls the venv interpreter directly, which is
rem  all `activate` really arranges for.
rem
rem    glasslinkxp list-windows
rem    glasslinkxp calibrate --display pfd
rem    glasslinkxp run
rem
rem  For the window over all of it, use glasslinkxp-gui.cmd (or `glasslinkxp gui`).
rem ---------------------------------------------------------------------------
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo(
    echo   No interpreter at %PY%
    echo(
    echo   Run install.cmd first.
    echo(
    exit /b 1
)
"%PY%" -m glasslinkxp.main %*
