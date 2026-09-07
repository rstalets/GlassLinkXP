<#
.SYNOPSIS
    Install GlassLinkXP: copies the app into %APPDATA%, sets up its Python
    environment, and adds a desktop shortcut to the window.

.DESCRIPTION
    This is the one-click installer double-clicking install.cmd runs. It:

      1. Copies pyproject.toml, uv.lock, .python-version, README.md, LICENSE
         and the whole src\ folder into %APPDATA%\GlassLinkXP, replacing
         anything already installed there (with a confirmation first).
      2. Runs `uv sync --locked` inside that folder, which provisions its own
         pinned Python and installs every dependency -- including the
         prebuilt Windows tesserocr wheel -- from the hash-checked lockfile.
      3. Fetches eng.traineddata (Tesseract's language data) and points
         TESSDATA_PREFIX at it for your Windows account.
      4. Adds a "GlassLinkXP" shortcut on the desktop, pointing at the window.

    There is no update mechanism yet: re-run this to reinstall, and it asks
    before replacing an existing install. Nothing outside %APPDATA%\GlassLinkXP
    is touched other than the desktop shortcut and the TESSDATA_PREFIX
    environment variable.

.PARAMETER Force
    Replace an existing install without asking first.

.PARAMETER SkipXPlanePlugin
    Do not offer to install the X-Plane side (XPPython3 + the dataref
    plugin). Without it, GlassLinkXP has nowhere to publish the labels to.

.EXAMPLE
    Double-click install.cmd, or:
    powershell -ExecutionPolicy Bypass -File install.ps1
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipXPlanePlugin
)

$ErrorActionPreference = 'Stop'
$SourceRoot = $PSScriptRoot
$Target = Join-Path $env:APPDATA 'GlassLinkXP'

function Write-Step  { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Fail        { param($m) Write-Host "`nFAILED: $m" -ForegroundColor Red; exit 1 }
function Test-Command { param($n) [bool](Get-Command $n -ErrorAction SilentlyContinue) }

# --------------------------------------------------------------------------
# 0. Sanity
# --------------------------------------------------------------------------
Write-Step 'Checking the installer folder'
foreach ($item in 'pyproject.toml', 'uv.lock', '.python-version', 'src') {
    if (-not (Test-Path (Join-Path $SourceRoot $item))) {
        Fail "$item is missing next to install.ps1. Re-extract the zip and run it from there."
    }
}
Write-Ok "installing from: $SourceRoot"

# --------------------------------------------------------------------------
# 1. Where it goes, and whether to replace what is already there
# --------------------------------------------------------------------------
if (Test-Path $Target) {
    if (-not $Force) {
        Write-Host "`nGlassLinkXP is already installed at:" -ForegroundColor Yellow
        Write-Host "    $Target"
        $answer = Read-Host 'Overwrite it? Your config.toml and calibration will be lost. [y/N]'
        if ($answer -notmatch '^[Yy]') {
            Write-Host 'Cancelled. Nothing was changed.'
            exit 0
        }
    }
    Write-Step "Removing the existing install"
    Remove-Item -Recurse -Force $Target
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null

# --------------------------------------------------------------------------
# 2. Copy the app in
# --------------------------------------------------------------------------
Write-Step "Copying GlassLinkXP to $Target"
foreach ($item in 'pyproject.toml', 'uv.lock', '.python-version', 'README.md', 'LICENSE') {
    $from = Join-Path $SourceRoot $item
    if (Test-Path $from) { Copy-Item $from (Join-Path $Target $item) }
}
Copy-Item (Join-Path $SourceRoot 'src') (Join-Path $Target 'src') -Recurse
Write-Ok 'copied'

# --------------------------------------------------------------------------
# 3. uv, then the interpreter and dependencies from the lockfile
# --------------------------------------------------------------------------
Write-Step 'Checking uv'
if (-not (Test-Command uv)) {
    Write-Host '    installing uv...'
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        if (Test-Command winget) {
            winget install --id astral-sh.uv -e --source winget `
                --accept-package-agreements --accept-source-agreements
        }
    }
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Test-Command uv)) {
        Fail 'could not install uv. See https://docs.astral.sh/uv/getting-started/installation/'
    }
}
Write-Ok "uv: $((uv --version) -join '')"

Push-Location $Target
try {
    Write-Step 'Installing the interpreter and dependencies from uv.lock'
    uv sync --locked --no-dev
    if ($LASTEXITCODE -ne 0) {
        Fail 'uv sync --locked failed. See the output above.'
    }
    $venvPython = Join-Path $Target '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) { Fail 'uv sync did not produce .venv\Scripts\python.exe' }
    Write-Ok "interpreter: $((& $venvPython -V) -join '')"

    # ----------------------------------------------------------------------
    # 4. Language data for Tesseract
    # ----------------------------------------------------------------------
    Write-Step 'Fetching the OCR language data (eng.traineddata)'
    # Pinned to a tag, not a branch: every OCR measurement this app ships with
    # was taken against this exact file. tessdata_fast at 4.1.0 is what
    # Debian/Ubuntu ship as tesseract-ocr-eng.
    $TessdataUrl    = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata'
    $TessdataSha256 = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
    $tessdataDir  = Join-Path $Target 'tessdata'
    $tessdataFile = Join-Path $tessdataDir 'eng.traineddata'
    New-Item -ItemType Directory -Force -Path $tessdataDir | Out-Null

    $needFetch = $true
    if (Test-Path $tessdataFile) {
        $have = (Get-FileHash -Path $tessdataFile -Algorithm SHA256).Hash.ToLower()
        if ($have -eq $TessdataSha256) { $needFetch = $false }
    }
    if ($needFetch) {
        Invoke-WebRequest -UseBasicParsing -Uri $TessdataUrl -OutFile $tessdataFile
        $got = (Get-FileHash -Path $tessdataFile -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $TessdataSha256) {
            Remove-Item $tessdataFile -Force -ErrorAction SilentlyContinue
            Fail "eng.traineddata did not match its recorded digest (expected $TessdataSha256, got $got)."
        }
    }
    Write-Ok "tessdata: $tessdataDir"
    [Environment]::SetEnvironmentVariable('TESSDATA_PREFIX', $tessdataDir, 'User')
    $env:TESSDATA_PREFIX = $tessdataDir
    # Broadcast the change so processes launched from the current Explorer
    # session (the desktop shortcut, right after this installer finishes)
    # see it without a sign-out -- Explorer otherwise keeps its own cached
    # copy of the environment until one of these arrives.
    try {
        Add-Type -Namespace GlassLinkXP -Name Env -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam,
    string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        [UIntPtr]$result = [UIntPtr]::Zero
        [GlassLinkXP.Env]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result) | Out-Null
    } catch {
        Write-Warn2 'could not notify running programs of the new environment variable.'
        Write-Warn2 'Sign out and back in if OCR reports a missing tessdata path.'
    }
    Write-Ok 'set TESSDATA_PREFIX for your account'

    # The GUI is Tk, part of the standard library but a separate build-time
    # component; uv's managed CPython ships it, but check anyway.
    & $venvPython -c "import tkinter" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'this Python has no Tk support -- the window will not open. The command'
        Write-Warn2 'line still works: src\glasslinkxp.cmd --help'
    }
}
finally { Pop-Location }

# --------------------------------------------------------------------------
# 5. Desktop shortcut
# --------------------------------------------------------------------------
Write-Step 'Adding a desktop shortcut'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'GlassLinkXP.lnk'
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $Target 'src\glasslinkxp-gui.cmd'
$shortcut.WorkingDirectory = $Target
$shortcut.Description = 'GlassLinkXP -- live G1000 softkey labels on a Stream Deck'
$shortcut.Save()
Write-Ok "shortcut: $shortcutPath"

# --------------------------------------------------------------------------
# 6. The X-Plane side (optional here -- the window can also do this)
# --------------------------------------------------------------------------
$pluginScript = Join-Path $Target 'src\scripts\install-xplane-plugin.ps1'
if (-not $SkipXPlanePlugin) {
    Write-Host "`nGlassLinkXP also needs a small plugin installed into X-Plane, so it has" -ForegroundColor Cyan
    Write-Host "somewhere to publish the labels to." -ForegroundColor Cyan
    $answer = Read-Host 'Install it now? [Y/n]'
    if ($answer -notmatch '^[Nn]') {
        # A separate powershell.exe, not `&`: install-xplane-plugin.ps1 calls
        # `exit` on failure, which would otherwise end this installer too.
        powershell -NoProfile -ExecutionPolicy Bypass -File $pluginScript
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "the X-Plane side did not complete."
            Write-Warn2 "run it again later: $pluginScript"
        }
    } else {
        Write-Warn2 "run this before flying: $pluginScript"
    }
}

Write-Host @"

============================================================
 Done. GlassLinkXP is installed at:
   $Target

 Open it from the "GlassLinkXP" shortcut on your desktop.
 The window will walk you through setup on first launch.
============================================================
"@ -ForegroundColor Green
