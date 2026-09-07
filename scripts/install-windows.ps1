<#
.SYNOPSIS
    Install the G1000 softkey daemon on Windows.

.DESCRIPTION
    Everything comes down as a pinned, hash-checked artefact. Nothing is
    compiled and nothing is vendored into this repository.

      1. uv provisions its own CPython (python-build-standalone) at the
         version in .python-version. `python-preference = "only-managed"` in
         pyproject.toml means it never falls back to a system interpreter, so
         the interpreter is as pinned as the dependencies.
      2. `uv sync --locked` installs everything from uv.lock, verifying every
         download against the SHA-256 recorded there. That includes the
         prebuilt Windows tesserocr wheel, which is not on PyPI and is fetched
         from its GitHub release by exact URL. The wheel bundles Tesseract
         5.5.2 and Leptonica 1.87.0, so **no separate Tesseract installation
         is needed** -- the UB Mannheim installer is no longer part of setup.
      3. eng.traineddata is fetched from a tagged upstream commit and checked
         against a recorded digest before use. It is language data, not a
         Python distribution, so uv.lock cannot cover it; this is the one
         download this script verifies itself.

    This replaces a vcpkg + MSVC build of Tesseract and Leptonica that took
    20-60 minutes and several GB. If you need that path back -- if the wheel
    is ever withdrawn -- it is in git history; see README.md, which records the
    commit.

.PARAMETER TessdataDir
    Where to put eng.traineddata. Default: tessdata\ under the repository.

.PARAMETER XPlanePath
    X-Plane 12 root. When given (or auto-detected), this script also runs
    scripts\install-xplane-plugin.ps1 to install XPPython3 and the dataref
    plugin. Without the sim side there is nothing for the daemon to publish
    into, because the Web API can write datarefs but cannot create them.

.PARAMETER SkipXPlane
    Install only the daemon; do not touch X-Plane.

.PARAMETER SkipTests
    Do not run the offline test suite at the end.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1

.NOTES
    Run from the repository root. Takes a couple of minutes.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$TessdataDir,
    [string]$XPlanePath,
    [switch]$SkipXPlane,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

# Language data for Tesseract. Pinned to a tag, not a branch: raw.github on
# `main` is a moving target, and the traineddata is a tuning input -- every
# OCR measurement and every number in the test suite was taken against this
# exact file. `tessdata_fast` is the variant Debian/Ubuntu ship as
# tesseract-ocr-eng, which is what the offline corpus was measured with.
# Changing it changes OCR behaviour; re-measure with `tune` before you do.
$TessdataUrl    = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata'
$TessdataSha256 = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
$TessdataBytes  = 4113088

function Write-Step  { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Fail        { param($m) Write-Host "`nFAILED: $m" -ForegroundColor Red; exit 1 }

function Test-Command { param($n) [bool](Get-Command $n -ErrorAction SilentlyContinue) }

# --------------------------------------------------------------------------
# 0. Sanity
# --------------------------------------------------------------------------
Write-Step 'Checking environment'

if ([Environment]::Is64BitOperatingSystem -ne $true) {
    Fail 'A 64-bit Windows install is required: the tesserocr wheel is win_amd64 only.'
}
foreach ($f in 'pyproject.toml', 'uv.lock', '.python-version') {
    if (-not (Test-Path (Join-Path $RepoRoot $f))) {
        Fail "$f not found in $RepoRoot. Run this from the repository root."
    }
}
Write-Ok "repository root: $RepoRoot"

# --------------------------------------------------------------------------
# 1. uv
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

Push-Location $RepoRoot
try {
    # ----------------------------------------------------------------------
    # 2. Interpreter + dependencies, from the lockfile
    # ----------------------------------------------------------------------
    Write-Step 'Installing the interpreter and dependencies from uv.lock'
    Write-Host '    uv downloads its own CPython and verifies every package against' -ForegroundColor DarkGray
    Write-Host '    the SHA-256 in uv.lock. A mismatch fails here rather than later.' -ForegroundColor DarkGray

    uv sync --locked
    if ($LASTEXITCODE -ne 0) {
        Fail @'
uv sync --locked failed.

If it reports that the lockfile is out of date, do NOT work around it by
dropping --locked: that is what makes the pinned hashes meaningful. Regenerate
with `uv lock`, inspect what moved, and commit the new lockfile.
'@
    }
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) { Fail 'uv sync did not produce .venv\Scripts\python.exe' }
    Write-Ok "interpreter: $((& $venvPython -V) -join '')"

    # ----------------------------------------------------------------------
    # 3. Language data
    # ----------------------------------------------------------------------
    Write-Step 'Fetching eng.traineddata'
    if (-not $TessdataDir) { $TessdataDir = Join-Path $RepoRoot 'tessdata' }
    New-Item -ItemType Directory -Force -Path $TessdataDir | Out-Null
    $tessdataFile = Join-Path $TessdataDir 'eng.traineddata'

    $needFetch = $true
    if (Test-Path $tessdataFile) {
        $have = (Get-FileHash -Path $tessdataFile -Algorithm SHA256).Hash.ToLower()
        if ($have -eq $TessdataSha256) {
            $needFetch = $false
            Write-Ok 'already present and matches the recorded digest'
        } else {
            Write-Warn2 'present but the digest does not match -- re-downloading'
        }
    }
    if ($needFetch) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $TessdataUrl -OutFile $tessdataFile
        } catch {
            Fail @"
Could not download eng.traineddata.

Fetch it manually into $TessdataDir :
  $TessdataUrl
It must be $TessdataBytes bytes, SHA-256 $TessdataSha256
"@
        }
        $got = (Get-FileHash -Path $tessdataFile -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $TessdataSha256) {
            Remove-Item $tessdataFile -Force -ErrorAction SilentlyContinue
            Fail @"
eng.traineddata did not match its recorded digest and has been deleted.

  expected $TessdataSha256
  got      $got

That means the file served at the pinned URL is not the one this install was
built and measured against. Do not work around this by deleting the check.
"@
        }
        Write-Ok "downloaded and verified ($TessdataBytes bytes)"
    }
    Write-Ok "tessdata: $TessdataDir"

    # ----------------------------------------------------------------------
    # 4. Point the config at it
    # ----------------------------------------------------------------------
    # ocr.tessdata_path is written absolute on purpose. Relative paths in
    # config.toml resolve against the config file, but only for the three
    # settings config.from_mapping rewrites; tessdata_path is passed through
    # verbatim, so a relative value would resolve against whatever directory
    # the daemon happened to be started from.
    $configPath = Join-Path $RepoRoot 'config.toml'
    if (-not (Test-Path $configPath)) {
        Copy-Item (Join-Path $RepoRoot 'config.example.toml') $configPath
        Write-Ok 'created config.toml from config.example.toml'
    }
    $tessdataToml = $TessdataDir -replace '\\', '/'
    $configText = Get-Content $configPath -Raw
    if ($configText -match '(?m)^\s*#?\s*tessdata_path\s*=') {
        $configText = $configText -replace '(?m)^\s*#?\s*tessdata_path\s*=.*$', "tessdata_path = `"$tessdataToml`""
    } else {
        $configText = $configText -replace '(?m)^\[ocr\]\s*$', "[ocr]`r`ntessdata_path = `"$tessdataToml`""
    }
    Set-Content -Path $configPath -Value $configText -Encoding UTF8
    Write-Ok "config.toml: ocr.tessdata_path = $tessdataToml"

    # ----------------------------------------------------------------------
    # 5. Verify
    # ----------------------------------------------------------------------
    Write-Step 'Verifying the installation'
    # Write the check to a file rather than passing it with -c. On Windows,
    # arguments reach a native executable as one command-line string, so a
    # multi-line snippet containing double quotes gets truncated mid-statement
    # ("SyntaxError: '(' was never closed"). A file has no quoting to get wrong.
    $checkPy = Join-Path ([System.IO.Path]::GetTempPath()) 'g1000_verify_tesserocr.py'
    $checkSrc = @'
import sys

import tesserocr
from PIL import Image, ImageDraw

print("    tesserocr", tesserocr.__version__)
print("    libtesseract", tesserocr.tesseract_version().splitlines()[0])

# Pass the tessdata directory explicitly, exactly as ocr.py does. Relying on
# TESSDATA_PREFIX alone fails with "invalid tessdata path: ./" when the
# variable is unset or points at the wrong level.
kwargs = {"psm": tesserocr.PSM.SINGLE_LINE}
tessdata = sys.argv[1] if len(sys.argv) > 1 else ""
if tessdata:
    kwargs["path"] = tessdata
print("    tessdata", tessdata or "(TESSDATA_PREFIX)")

img = Image.new("L", (240, 64), 255)
ImageDraw.Draw(img).text((12, 16), "INSET", fill=0)

try:
    api = tesserocr.PyTessBaseAPI(**kwargs)
except RuntimeError as exc:
    raise SystemExit(
        "could not initialise Tesseract ({}). The wheel installed, but the "
        "language data was not found: point ocr.tessdata_path at a directory "
        "containing eng.traineddata.".format(exc)
    )
try:
    api.SetVariable("tessedit_char_whitelist", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    api.SetImage(img)
    got = api.GetUTF8Text().strip()
finally:
    api.End()

print("    round-trip OCR ->", repr(got))
if not got:
    raise SystemExit("OCR returned nothing; check the tessdata directory.")
'@
    # ASCII, so no BOM ends up in front of the first statement.
    Set-Content -Path $checkPy -Value $checkSrc -Encoding ASCII
    try {
        & $venvPython $checkPy $TessdataDir
        $checkExit = $LASTEXITCODE
    } finally {
        Remove-Item $checkPy -Force -ErrorAction SilentlyContinue
    }
    if ($checkExit -ne 0) { Fail 'tesserocr imported or ran incorrectly. See the error above.' }
    Write-Ok 'tesserocr works'

    # The GUI is Tk. uv's python-build-standalone builds include it (verified),
    # but check here anyway: a missing Tk fails only when the window is asked
    # for, which is a double-click that appears to do nothing.
    & $venvPython -c "import tkinter" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'this Python has no Tk support, so g1000-gui.cmd will not open.'
        Write-Warn2 'Everything works from the command line regardless.'
    } else {
        Write-Ok 'Tk found -- the graphical interface will open'
    }

    if ($SkipTests) {
        Write-Step 'Skipping the offline test suite (-SkipTests)'
    } else {
        Write-Step 'Running the offline test suite'
        & $venvPython -m pytest -q
        if ($LASTEXITCODE -ne 0) { Write-Warn2 'some tests failed -- see output above' }
        else { Write-Ok 'tests passed' }
    }
}
finally { Pop-Location }

# --------------------------------------------------------------------------
# 6. The X-Plane side
# --------------------------------------------------------------------------
$simInstalled = $false
if ($SkipXPlane) {
    Write-Step 'Skipping the X-Plane side (-SkipXPlane)'
    Write-Warn2 'remember to run scripts\install-xplane-plugin.ps1 -- without the'
    Write-Warn2 'plugin the datarefs do not exist and the daemon has nowhere to publish.'
} else {
    Write-Step 'Installing the X-Plane side (XPPython3 + dataref plugin)'
    $simArgs = @()
    if ($XPlanePath) { $simArgs += @('-XPlanePath', $XPlanePath) }
    try {
        & (Join-Path $PSScriptRoot 'install-xplane-plugin.ps1') @simArgs
        if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) { $simInstalled = $true }
    } catch {
        Write-Warn2 "the X-Plane side did not complete: $_"
        Write-Warn2 'the daemon is still installed; re-run scripts\install-xplane-plugin.ps1 on its own.'
    }
}

Write-Host @"

============================================================
 Done. To use it:

   .\g1000-gui                     <- the window; start here

 or from the command line:

   .\g1000 list-windows
   .\g1000 calibrate --display pfd
   .\g1000 run

 Both .cmd files call the venv interpreter directly, so there is
 no venv to activate and no execution policy to argue with. (If
 you prefer, .venv\Scripts\Activate.ps1 still works.)
"@ -ForegroundColor Green

if (-not $simInstalled -and -not $SkipXPlane) {
    Write-Warn2 'the X-Plane side did not install -- the datarefs will not exist until it does.'
}
