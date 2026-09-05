<#
.SYNOPSIS
    Install XPPython3 and the G1000 softkey dataref plugin into X-Plane 12.

.DESCRIPTION
    The daemon reads pixels and publishes labels, but something has to create
    the datarefs it writes into -- the X-Plane Web API can write a dataref but
    cannot create one. That is all PI_G1000SoftkeyLabels.py does.

    Three things have to be true before the plugin runs:

      1. XPPython3 is installed in <X-Plane>/Resources/plugins/XPPython3.
         Version 4 bundles its own Python 3.12, so no system Python is needed
         and the plugin does NOT run in this project's venv -- which is why it
         imports nothing but the standard library and the XPPython3 API.
      2. <X-Plane>/Resources/plugins/PythonPlugins exists. XPPython3 creates
         it on the first X-Plane run, so on a fresh install it is not there
         yet. This script creates it if needed, which is harmless.
      3. PI_G1000SoftkeyLabels.py sits in that folder. XPPython3 loads plugins
         by the PI_ prefix.

.PARAMETER XPlanePath
    X-Plane 12 root. Auto-detected from %LOCALAPPDATA%\x-plane_install_12.txt
    and the usual install locations if omitted.

.PARAMETER Beta
    Install the XPPython3 beta build instead of stable.

.PARAMETER SkipXPPython3
    Only copy the plugin; assume XPPython3 is already installed.

.PARAMETER Force
    Reinstall XPPython3 even if it is already present.

.PARAMETER VerifyOnly
    Skip installing. Ask a RUNNING X-Plane whether the 48 datarefs exist.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-xplane-plugin.ps1

.EXAMPLE
    # after starting X-Plane, confirm the datarefs actually registered
    powershell -ExecutionPolicy Bypass -File scripts\install-xplane-plugin.ps1 -VerifyOnly
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$XPlanePath,
    [switch]$Beta,
    [switch]$SkipXPPython3,
    [switch]$Force,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step  { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Fail        { param($m) Write-Host "`nFAILED: $m" -ForegroundColor Red; exit 1 }

# --------------------------------------------------------------------------
# Locate X-Plane 12
# --------------------------------------------------------------------------
function Test-XPlaneRoot {
    param($Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path $Path)) { return $false }
    return (Test-Path (Join-Path $Path 'Resources\plugins')) -and
           ((Test-Path (Join-Path $Path 'X-Plane.exe')) -or (Test-Path (Join-Path $Path 'X-Plane 12.exe')))
}

function Find-XPlane {
    $found = New-Object System.Collections.Generic.List[string]

    # X-Plane's own installer leaves a record of every install location here.
    $record = Join-Path $env:LOCALAPPDATA 'x-plane_install_12.txt'
    if (Test-Path $record) {
        foreach ($line in (Get-Content $record -ErrorAction SilentlyContinue)) {
            $p = $line.Trim()
            if ($p -and (Test-XPlaneRoot $p)) { $found.Add((Resolve-Path $p).Path) }
        }
    }
    foreach ($c in @(
        'C:\X-Plane 12',
        (Join-Path ${env:ProgramFiles(x86)} 'Steam\steamapps\common\X-Plane 12'),
        (Join-Path $env:ProgramFiles 'Steam\steamapps\common\X-Plane 12'),
        'D:\X-Plane 12',
        'D:\Steam\steamapps\common\X-Plane 12',
        'D:\SteamLibrary\steamapps\common\X-Plane 12'
    )) {
        if ((Test-XPlaneRoot $c) -and -not $found.Contains((Resolve-Path $c).Path)) {
            $found.Add((Resolve-Path $c).Path)
        }
    }
    return $found
}

Write-Step 'Locating X-Plane 12'
if ($XPlanePath) {
    if (-not (Test-XPlaneRoot $XPlanePath)) {
        Fail "'$XPlanePath' does not look like an X-Plane 12 root (no Resources\plugins and no X-Plane executable)."
    }
    $XPlanePath = (Resolve-Path $XPlanePath).Path
} else {
    $hits = Find-XPlane
    if ($hits.Count -eq 0) {
        Fail @'
Could not find X-Plane 12. Pass it explicitly:
  scripts\install-xplane-plugin.ps1 -XPlanePath "D:\X-Plane 12"
'@
    }
    if ($hits.Count -gt 1) {
        Write-Warn2 "found $($hits.Count) installs:"
        $hits | ForEach-Object { Write-Host "        $_" }
        Write-Warn2 'using the first. Pass -XPlanePath to choose another.'
    }
    $XPlanePath = $hits[0]
}
Write-Ok "X-Plane 12: $XPlanePath"

$pluginsDir  = Join-Path $XPlanePath 'Resources\plugins'
$xp3Dir      = Join-Path $pluginsDir 'XPPython3'
$pyPluginDir = Join-Path $pluginsDir 'PythonPlugins'
$sourcePlug  = Join-Path $RepoRoot 'xppython3\PI_G1000SoftkeyLabels.py'

# --------------------------------------------------------------------------
# Verify-only: ask a running X-Plane whether the datarefs exist
# --------------------------------------------------------------------------
function Invoke-Verify {
    Write-Step 'Asking X-Plane whether the datarefs exist'
    # 24 label (byte array) datarefs + 24 /bg (int) background-colour datarefs.
    $names = @()
    foreach ($d in @('pfd','mfd')) {
        1..12 | ForEach-Object {
            $names += "g1000/softkey/$d/$_"
            $names += "g1000/softkey/$d/$_/bg"
        }
    }

    $ok = $false
    foreach ($v in @('v2','v1')) {
        $q = ($names | ForEach-Object { 'filter%5Bname%5D=' + [uri]::EscapeDataString($_) }) -join '&'
        $url = "http://localhost:8086/api/$v/datarefs?$q"
        try {
            $resp = Invoke-RestMethod -Uri $url -TimeoutSec 5 -ErrorAction Stop
        } catch {
            continue
        }
        $ok = $true
        $got = @($resp.data)
        Write-Ok "web API $v responded"
        if ($got.Count -eq 0) {
            Write-Warn2 'X-Plane is running but none of the 48 datarefs exist.'
            Write-Warn2 'The plugin did not load. Check Log.txt and XPPython3.log in the X-Plane root.'
            return $false
        }
        Write-Ok "$($got.Count)/48 datarefs registered"
        $got | Select-Object -First 3 | ForEach-Object {
            Write-Host "        $($_.name)  id=$($_.id)  type=$($_.value_type)"
        }
        # A label must be a byte array and a /bg must be an int: the daemon
        # sends base64 to one and a bare number to the other, so a dataref
        # registered as the wrong type rejects every write it ever gets.
        $wrongLabel = @($got | Where-Object { $_.name -notlike '*/bg' -and $_.value_type -ne 'data' })
        if ($wrongLabel) {
            Write-Warn2 "expected value_type 'data' (byte array) for $($wrongLabel[0].name); got: $($wrongLabel[0].value_type)"
        }
        $wrongBg = @($got | Where-Object { $_.name -like '*/bg' -and $_.value_type -ne 'int' })
        if ($wrongBg) {
            Write-Warn2 "expected value_type 'int' for $($wrongBg[0].name); got: $($wrongBg[0].value_type)"
        }
        if ($got.Count -lt 48) { Write-Warn2 'fewer than 48 -- the plugin may have failed partway.' }
        return ($got.Count -eq 48)
    }
    if (-not $ok) {
        Write-Warn2 'No response from http://localhost:8086.'
        Write-Warn2 'X-Plane must be RUNNING, and needs to be 12.1.1 or newer for the web API.'
        Write-Warn2 'If it is running, the web server may be off (--no_web_server) or on another port.'
    }
    return $false
}

if ($VerifyOnly) {
    if (Invoke-Verify) {
        Write-Host "`nAll 48 datarefs are live. PilotsDeck address: g1000/softkey/pfd/1:s64" -ForegroundColor Green
        exit 0
    }
    exit 1
}

# --------------------------------------------------------------------------
# XPPython3
# --------------------------------------------------------------------------
Write-Step 'Installing XPPython3'
if ((Test-Path $xp3Dir) -and -not $Force) {
    Write-Ok "already present: $xp3Dir (use -Force to reinstall)"
} elseif ($SkipXPPython3) {
    Write-Warn2 'skipping XPPython3 (-SkipXPPython3)'
    if (-not (Test-Path $xp3Dir)) { Write-Warn2 "but $xp3Dir does not exist -- the plugin will not load." }
} else {
    # v4 bundles Python 3.12; no system Python needed.
    $zipName = if ($Beta) { 'xp3-win32b.zip' } else { 'xp3-win32.zip' }
    $url     = "https://maps.avnwx.com/data/x-plane/$zipName"
    $tmp     = Join-Path ([System.IO.Path]::GetTempPath()) $zipName
    Write-Host "    downloading $url"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp
    } catch {
        Fail @"
Could not download XPPython3 from $url

Download it by hand and extract into:
  $pluginsDir
so that you end up with $xp3Dir
  https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html
"@
    }
    if ($Force -and (Test-Path $xp3Dir)) {
        Write-Host '    removing the existing XPPython3 folder...'
        Remove-Item $xp3Dir -Recurse -Force
    }
    Write-Host "    extracting into $pluginsDir"
    Expand-Archive -Path $tmp -DestinationPath $pluginsDir -Force
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $xp3Dir)) {
        Fail "extraction did not produce $xp3Dir -- the zip layout may have changed."
    }
    Write-Ok "installed: $xp3Dir"
}

# --------------------------------------------------------------------------
# PythonPlugins (normally created by XPPython3 on the first X-Plane run)
# --------------------------------------------------------------------------
Write-Step 'Preparing the PythonPlugins folder'
if (Test-Path $pyPluginDir) {
    Write-Ok "exists: $pyPluginDir"
} else {
    New-Item -ItemType Directory -Force -Path $pyPluginDir | Out-Null
    Write-Ok "created: $pyPluginDir"
    Write-Host '        (XPPython3 would create this on the first X-Plane run;' -ForegroundColor DarkGray
    Write-Host '         making it early is harmless and saves a launch cycle)'  -ForegroundColor DarkGray
}

# --------------------------------------------------------------------------
# The plugin itself
# --------------------------------------------------------------------------
Write-Step 'Installing PI_G1000SoftkeyLabels.py'
if (-not (Test-Path $sourcePlug)) { Fail "plugin source not found: $sourcePlug" }
$dest = Join-Path $pyPluginDir 'PI_G1000SoftkeyLabels.py'
if ((Test-Path $dest) -and -not $Force) {
    $a = (Get-FileHash $sourcePlug -Algorithm SHA256).Hash
    $b = (Get-FileHash $dest       -Algorithm SHA256).Hash
    if ($a -eq $b) { Write-Ok 'already installed and identical' }
    else { Copy-Item $sourcePlug $dest -Force; Write-Ok "updated: $dest" }
} else {
    Copy-Item $sourcePlug $dest -Force
    Write-Ok "installed: $dest"
}

# --------------------------------------------------------------------------
# Next steps
# --------------------------------------------------------------------------
$running = @(Get-Process -Name 'X-Plane*' -ErrorAction SilentlyContinue)
Write-Host @"

============================================================
 X-Plane side installed.

   XPPython3 : $xp3Dir
   Plugin    : $dest

 Next:
   1. $(if ($running) { 'RESTART X-Plane (it is running now -- plugins load at startup)' } else { 'Start X-Plane 12 and load an aircraft with a G1000' })
   2. Confirm the 48 datarefs registered:
        scripts\install-xplane-plugin.ps1 -VerifyOnly
   3. Pop out the PFD and MFD into their own windows, then:
        .\g1000 list-windows
        .\g1000 calibrate --display pfd

 If the plugin does not load, look in the X-Plane root at:
   Log.txt  and  XPPython3.log
============================================================
"@ -ForegroundColor Green
