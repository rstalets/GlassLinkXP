<#
.SYNOPSIS
    Install XPPython3 and the G1000 softkey dataref plugin into X-Plane 12.

.DESCRIPTION
    The daemon reads pixels and publishes labels, but something has to create
    the datarefs it writes into -- the X-Plane Web API can write a dataref but
    cannot create one. That is all PI_GlassLinkXP.py does.

    Three things have to be true before the plugin runs:

      1. XPPython3 is installed in <X-Plane>/Resources/plugins/XPPython3.
         It is what loads Python plugins at all, so this script checks for it
         first and offers to download and install it if it is not there --
         before copying our plugin in, since a PI_ file with no XPPython3 to
         load it does nothing at all. Version 4 bundles its own Python 3.12,
         so no system Python is needed and the plugin does NOT run in this
         project's venv -- which is why it imports nothing but the standard
         library and the XPPython3 API.
      2. <X-Plane>/Resources/plugins/PythonPlugins exists. XPPython3 creates
         it on the first X-Plane run, so on a fresh install it is not there
         yet. This script creates it if needed, which is harmless.
      3. PI_GlassLinkXP.py sits in that folder. XPPython3 loads plugins
         by the PI_ prefix.

.PARAMETER XPlanePath
    X-Plane 12 root. Auto-detected from %LOCALAPPDATA%\x-plane_install_12.txt
    and the usual install locations if omitted.

.PARAMETER Beta
    Install the XPPython3 beta build instead of stable.

.PARAMETER SkipXPPython3
    Only copy the plugin; assume XPPython3 is already installed. Without it,
    a missing XPPython3 is offered for download instead.

.PARAMETER Yes
    Do not ask before downloading XPPython3 -- install it if it is missing.
    install.ps1 passes this because it has already asked.

.PARAMETER Force
    Reinstall XPPython3 even if it is already present.

.PARAMETER VerifyOnly
    Skip installing. Ask a RUNNING X-Plane whether the 48 datarefs exist.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File src\scripts\install-xplane-plugin.ps1

.EXAMPLE
    # after starting X-Plane, confirm the datarefs actually registered
    powershell -ExecutionPolicy Bypass -File src\scripts\install-xplane-plugin.ps1 -VerifyOnly
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$XPlanePath,
    [switch]$Beta,
    [switch]$SkipXPPython3,
    [switch]$Force,
    [switch]$VerifyOnly,
    [switch]$Yes
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
  src\scripts\install-xplane-plugin.ps1 -XPlanePath "D:\X-Plane 12"
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
$sourcePlug  = Join-Path $RepoRoot 'xppython3\PI_GlassLinkXP.py'

# --------------------------------------------------------------------------
# Verify-only: ask a running X-Plane whether the datarefs exist
# --------------------------------------------------------------------------
function Invoke-Verify {
    Write-Step 'Asking X-Plane whether the datarefs exist'
    # 24 label (byte array) datarefs + 24 /bg (int) background-colour datarefs.
    $names = @()
    foreach ($d in @('pfd','mfd')) {
        1..12 | ForEach-Object {
            $names += "glasslinkxp/softkey/$d/$_"
            $names += "glasslinkxp/softkey/$d/$_/bg"
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
        Write-Host "`nAll 48 datarefs are live. PilotsDeck address: glasslinkxp/softkey/pfd/1:s16" -ForegroundColor Green
        exit 0
    }
    exit 1
}

# --------------------------------------------------------------------------
# XPPython3
# --------------------------------------------------------------------------
# GlassLinkXP's plugin is a *Python* plugin, and X-Plane cannot run one on its
# own: XPPython3 is the host that loads it. So this checks whether it is
# already there and, if it is not, offers to fetch it -- before our own plugin
# is copied in, because a PI_ file in PythonPlugins with no XPPython3 to load
# it is a file that silently does nothing.
#
# Presence is decided by finding an .xpl, not by the folder existing: a folder
# left behind by an interrupted extraction would otherwise be read as an
# install, and the failure that follows is X-Plane quietly not loading
# anything, which is a bad thing to have to debug from the other end.
Write-Step 'Checking for XPPython3'
$xp3Xpl = $null
if (Test-Path $xp3Dir) {
    $xp3Xpl = Get-ChildItem -Path $xp3Dir -Filter '*.xpl' -Recurse -File -ErrorAction SilentlyContinue |
              Select-Object -First 1
}
$zipName = if ($Beta) { 'xp3-win32b.zip' } else { 'xp3-win32.zip' }
# The download location named by the XPPython3 documentation itself
# (xppython3.readthedocs.io -> Plugin Installation). v4 bundles its own
# Python 3.12, which is why the plugin needs no system Python and imports
# nothing outside the standard library.
$url = "https://maps.avnwx.com/data/x-plane/$zipName"

$installXp3 = $false
if ($xp3Xpl -and -not $Force) {
    Write-Ok "already installed: $($xp3Xpl.FullName)"
    Write-Host '        (use -Force to reinstall it)' -ForegroundColor DarkGray
} elseif ($SkipXPPython3) {
    Write-Warn2 'skipping XPPython3 (-SkipXPPython3)'
    if (-not $xp3Xpl) { Write-Warn2 "but no XPPython3 plugin is installed -- our plugin will not load." }
} else {
    if (-not $xp3Xpl -and (Test-Path $xp3Dir)) {
        Write-Warn2 "$xp3Dir exists but holds no .xpl -- treating XPPython3 as not installed."
    }
    if ($Yes -or $Force) {
        # install.ps1 passes -Yes because it has already asked; -Force is an
        # explicit "reinstall it", which is an answer in itself.
        $installXp3 = $true
    } else {
        Write-Host ''
        Write-Host '    XPPython3 is not installed in this copy of X-Plane. It is what runs' -ForegroundColor Yellow
        Write-Host '    Python plugins, including the small one GlassLinkXP publishes into,' -ForegroundColor Yellow
        Write-Host '    and it bundles its own Python -- nothing else on your PC is touched.' -ForegroundColor Yellow
        Write-Host "    It would be downloaded from:"
        Write-Host "        $url"
        Write-Host "    and extracted into:"
        Write-Host "        $pluginsDir"
        $answer = Read-Host '    Download and install XPPython3 now? [Y/n]'
        $installXp3 = ($answer -notmatch '^[Nn]')
    }
    if (-not $installXp3) {
        Write-Warn2 'not installing XPPython3.'
        Write-Warn2 'GlassLinkXP''s plugin will still be copied in, but nothing will load it'
        Write-Warn2 'until XPPython3 is there. Install it by hand from:'
        Write-Warn2 '  https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html'
        Write-Warn2 "extracting into $pluginsDir, or re-run this script and answer yes."
    }
}

if ($installXp3) {
    Write-Step 'Installing XPPython3'
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) $zipName
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
    $xp3Xpl = Get-ChildItem -Path $xp3Dir -Filter '*.xpl' -Recurse -File -ErrorAction SilentlyContinue |
              Select-Object -First 1
    if (-not $xp3Xpl) {
        Fail "extracted $xp3Dir but it holds no .xpl -- the zip layout may have changed."
    }
    Write-Ok "installed: $($xp3Xpl.FullName)"
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
Write-Step 'Installing PI_GlassLinkXP.py'
if (-not (Test-Path $sourcePlug)) { Fail "plugin source not found: $sourcePlug" }
$dest = Join-Path $pyPluginDir 'PI_GlassLinkXP.py'
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
# The plugin this one was renamed from
# --------------------------------------------------------------------------
# XPPython3 loads every PI_*.py it finds, so leaving the old file behind means
# both plugins load: the old one goes on registering the g1000/softkey/*
# datarefs this no longer writes to, which is stale state to debug against
# rather than a clean upgrade.
$legacyPlug = Join-Path $pyPluginDir 'PI_G1000SoftkeyLabels.py'
if (Test-Path $legacyPlug) {
    Write-Step 'Removing the plugin this replaces'
    Remove-Item $legacyPlug -Force
    Write-Ok "removed: $legacyPlug (it registered the old g1000/softkey/* datarefs)"
    Write-Warn2 'any Stream Deck buttons still addressing g1000/softkey/... need'
    Write-Warn2 'changing to glasslinkxp/softkey/... -- see README.md.'
}

# --------------------------------------------------------------------------
# Next steps
# --------------------------------------------------------------------------
$running = @(Get-Process -Name 'X-Plane*' -ErrorAction SilentlyContinue)
Write-Host @"

============================================================
 $(if ($xp3Xpl) { 'X-Plane side installed.' } else { 'X-Plane side installed, but it will not load yet.' })

   XPPython3 : $(if ($xp3Xpl) { $xp3Dir } else { 'NOT INSTALLED -- nothing will load the plugin until it is' })
   Plugin    : $dest

 Next:
   1. $(if (-not $xp3Xpl) { 'Install XPPython3: https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html' } elseif ($running) { 'RESTART X-Plane (it is running now -- plugins load at startup)' } else { 'Start X-Plane 12 and load an aircraft with a G1000' })
   2. Confirm the 48 datarefs registered:
        src\scripts\install-xplane-plugin.ps1 -VerifyOnly
   3. Pop out the PFD and MFD into their own windows, then:
        .\glasslinkxp list-windows
        .\glasslinkxp calibrate

 If the plugin does not load, look in the X-Plane root at:
   Log.txt  and  XPPython3.log
============================================================
"@ -ForegroundColor Green
