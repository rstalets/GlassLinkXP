<#
.SYNOPSIS
    Install the G1000 softkey daemon on Windows with a compiled tesserocr.

.DESCRIPTION
    tesserocr publishes no Windows wheel, so pip/uv must compile it from the
    sdist. That needs three things the UB-Mannheim Tesseract installer does not
    provide:

      1. Tesseract + Leptonica *development* files (headers and .lib import
         libraries). This script builds them with vcpkg.
      2. The LIBPATH and INCLUDE environment variables pointing at them --
         tesserocr's setup.py reads those two variables specifically, which is
         why adding Tesseract to PATH does nothing.
      3. An MSVC toolchain to compile the Cython extension.

    Two further traps this script handles:

      * tesserocr's setup.py filters candidate .lib files to those whose path
        contains the major+minor digits from `tesseract -v` (e.g. "55" for
        5.5.x). We put vcpkg's own tesseract.exe first on PATH so the detected
        version always matches the library we just built.
      * tesserocr has no [build-system] table in pyproject.toml, so Cython is
        only declared via the legacy setup_requires. Under PEP 517 build
        isolation it is absent and the build fails. We pre-install the build
        dependencies and pass --no-build-isolation.

.PARAMETER VcpkgRoot
    Where to clone/find vcpkg. Default C:\vcpkg.

.PARAMETER PythonVersion
    Python version for the venv, fetched by uv. Default 3.12.

.PARAMETER TessdataPrefix
    Directory containing eng.traineddata. Auto-detected if omitted.

.PARAMETER SkipVcpkg
    Reuse an existing vcpkg tesseract build without re-running vcpkg install.

.PARAMETER XPlanePath
    X-Plane 12 root. When given (or auto-detected), this script also runs
    scripts\install-xplane-plugin.ps1 to install XPPython3 and the dataref
    plugin. Without the sim side there is nothing for the daemon to publish
    into, because the Web API can write datarefs but cannot create them.

.PARAMETER SkipXPlane
    Install only the daemon; do not touch X-Plane.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1

.NOTES
    Run from the repository root. Takes 20-60 minutes on first run, almost all
    of it vcpkg compiling Tesseract, Leptonica and their dependencies.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$VcpkgRoot     = 'C:\vcpkg',
    [string]$PythonVersion = '3.12',
    [string]$TessdataPrefix,
    [string]$XPlanePath,
    [switch]$SkipVcpkg,
    [switch]$SkipXPlane
)

$ErrorActionPreference = 'Stop'
$Triplet  = 'x64-windows'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step  { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Fail        { param($m) Write-Host "`nFAILED: $m" -ForegroundColor Red; exit 1 }

function Test-Command { param($n) [bool](Get-Command $n -ErrorAction SilentlyContinue) }

# --------------------------------------------------------------------------
# 0. Sanity
# --------------------------------------------------------------------------
Write-Step 'Checking environment'

if ([Environment]::Is64BitOperatingSystem -ne $true) { Fail 'A 64-bit Windows install is required.' }
if (-not (Test-Path (Join-Path $RepoRoot 'requirements.txt'))) {
    Fail "requirements.txt not found in $RepoRoot. Run this from the repository root."
}
Write-Ok "repository root: $RepoRoot"

# --------------------------------------------------------------------------
# 1. git
# --------------------------------------------------------------------------
Write-Step 'Checking git'
if (-not (Test-Command git)) {
    if (Test-Command winget) {
        Write-Host '    installing git via winget...'
        winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path','User')
    }
    if (-not (Test-Command git)) { Fail 'git is required. Install it from https://git-scm.com/download/win' }
}
Write-Ok "git: $((git --version) -join '')"

# --------------------------------------------------------------------------
# 2. MSVC build tools
# --------------------------------------------------------------------------
Write-Step 'Locating the MSVC C++ toolchain'

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
function Find-VcVars {
    if (-not (Test-Path $vswhere)) { return $null }
    $path = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath 2>$null | Select-Object -First 1
    if (-not $path) { return $null }
    $bat = Join-Path $path 'VC\Auxiliary\Build\vcvars64.bat'
    if (Test-Path $bat) { return $bat } else { return $null }
}

$vcvars = Find-VcVars
if (-not $vcvars) {
    Write-Warn2 'MSVC C++ tools not found.'
    if (Test-Command winget) {
        Write-Host '    installing Visual Studio 2022 Build Tools (this takes a while)...'
        winget install --id Microsoft.VisualStudio.2022.BuildTools -e --source winget `
            --accept-package-agreements --accept-source-agreements `
            --override '--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended'
        $vcvars = Find-VcVars
    }
    if (-not $vcvars) {
        Fail @'
Install "Build Tools for Visual Studio 2022" with the
"Desktop development with C++" workload, then re-run this script:
  https://visualstudio.microsoft.com/downloads/  (under "Tools for Visual Studio")
'@
    }
}
Write-Ok "vcvars64: $vcvars"

# --------------------------------------------------------------------------
# 3. vcpkg -> Tesseract + Leptonica development files
# --------------------------------------------------------------------------
Write-Step 'Building Tesseract development libraries with vcpkg'

if (-not (Test-Path $VcpkgRoot)) {
    Write-Host "    cloning vcpkg into $VcpkgRoot ..."
    git clone --depth 1 https://github.com/microsoft/vcpkg $VcpkgRoot
}
$vcpkgExe = Join-Path $VcpkgRoot 'vcpkg.exe'
if (-not (Test-Path $vcpkgExe)) {
    Write-Host '    bootstrapping vcpkg...'
    & (Join-Path $VcpkgRoot 'bootstrap-vcpkg.bat') -disableMetrics
    if (-not (Test-Path $vcpkgExe)) { Fail 'vcpkg bootstrap did not produce vcpkg.exe.' }
}
Write-Ok "vcpkg: $vcpkgExe"

$installed = Join-Path $VcpkgRoot "installed\$Triplet"
$libDir    = Join-Path $installed 'lib'
$incDir    = Join-Path $installed 'include'
$binDir    = Join-Path $installed 'bin'

if (-not $SkipVcpkg) {
    Write-Host "    vcpkg install tesseract:$Triplet  (pulls in leptonica; 20-60 min first time)"
    & $vcpkgExe install "tesseract:$Triplet" --disable-metrics
    if ($LASTEXITCODE -ne 0) { Fail "vcpkg install tesseract:$Triplet failed." }
} else {
    Write-Warn2 'skipping vcpkg install (-SkipVcpkg)'
}

foreach ($d in @($libDir, $incDir, $binDir)) {
    if (-not (Test-Path $d)) { Fail "expected vcpkg directory missing: $d" }
}
if (-not (Test-Path (Join-Path $incDir 'tesseract\baseapi.h'))) {
    Fail "tesseract headers not found under $incDir"
}
Write-Ok "development files: $installed"

# --------------------------------------------------------------------------
# 4. Version-digit match (the silent killer)
# --------------------------------------------------------------------------
Write-Step 'Checking the Tesseract library version digits'

# Put vcpkg's tesseract.exe first so `tesseract -v` reports the version of the
# library we just built, which is what setup.py filters .lib names against.
$toolsDir = Join-Path $installed 'tools\tesseract'
if (Test-Path $toolsDir) { $env:Path = "$toolsDir;$binDir;$env:Path" }

$tessLibs = @(Get-ChildItem -Path $libDir -Filter 'tesseract*.lib' -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -notlike '*d.lib' })
if (-not $tessLibs) { Fail "no tesseract*.lib found in $libDir" }
$leptLibs = @(Get-ChildItem -Path $libDir -Filter 'lept*.lib' -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -notlike '*d.lib' })
if (-not $leptLibs) { Fail "no lept*.lib found in $libDir" }

$tessLib = $tessLibs[0].Name
Write-Ok "tesseract import library: $tessLib"
Write-Ok "leptonica import library: $($leptLibs[0].Name)"

$detected = $null
try {
    $vout = (& tesseract -v 2>&1) -join "`n"
    if ($vout -match 'tesseract\s+v?((?:\d+\.)+\d+)') { $detected = $Matches[1] }
} catch { }

if ($detected) {
    $digits = ($detected -split '\.')[0..1] -join ''
    Write-Ok "tesseract -v reports $detected (setup.py will look for '$digits' in the .lib path)"
    if ($tessLib -notmatch $digits -and $libDir -notmatch $digits) {
        Write-Warn2 "version mismatch: '$tessLib' does not contain '$digits'."
        Write-Warn2 "setup.py will reject it. Another tesseract.exe is probably shadowing vcpkg's."
    }
} else {
    Write-Warn2 'could not run `tesseract -v`; setup.py will fall back to its minimum version.'
}

# --------------------------------------------------------------------------
# 5. Import the MSVC environment, then append vcpkg's paths
# --------------------------------------------------------------------------
Write-Step 'Importing the MSVC build environment'

# vcvars64 sets INCLUDE/LIB/LIBPATH for the toolchain itself. We must APPEND to
# those, never replace them -- replacing INCLUDE loses stdio.h and friends.
$envDump = cmd /c "call `"$vcvars`" >nul 2>&1 && set"
if ($LASTEXITCODE -ne 0) { Fail 'vcvars64.bat failed.' }
foreach ($line in $envDump) {
    if ($line -match '^([^=]+)=(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] -ErrorAction SilentlyContinue
    }
}
Write-Ok 'MSVC environment loaded'

foreach ($pair in @(@('INCLUDE', $incDir), @('LIB', $libDir), @('LIBPATH', $libDir))) {
    $name, $dir = $pair
    $cur = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ([string]::IsNullOrEmpty($cur)) { $new = $dir } else { $new = "$cur;$dir" }
    Set-Item -Path "env:$name" -Value $new
}
Write-Ok "INCLUDE += $incDir"
Write-Ok "LIB, LIBPATH += $libDir"

# --------------------------------------------------------------------------
# 6. uv
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
    if (-not (Test-Command uv)) { Fail 'could not install uv. See https://docs.astral.sh/uv/getting-started/installation/' }
}
Write-Ok "uv: $((uv --version) -join '')"

# --------------------------------------------------------------------------
# 7. Virtual environment
# --------------------------------------------------------------------------
Write-Step "Creating the virtual environment (Python $PythonVersion)"
Push-Location $RepoRoot
try {
    uv venv --python $PythonVersion
    if ($LASTEXITCODE -ne 0) { Fail "uv venv --python $PythonVersion failed." }
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) { Fail 'uv venv did not produce .venv\Scripts\python.exe' }
    $env:VIRTUAL_ENV = Join-Path $RepoRoot '.venv'
    Write-Ok ".venv ready: $((& $venvPython --version) -join '')"

    # ----------------------------------------------------------------------
    # 8. Build dependencies, then tesserocr WITHOUT build isolation
    # ----------------------------------------------------------------------
    Write-Step 'Installing build dependencies'
    # tesserocr declares Cython only through the legacy setup_requires, so an
    # isolated PEP 517 build environment would not contain it.
    uv pip install --python $venvPython 'setuptools>=68' wheel 'Cython>=3.0.0,<3.2.0' cysignals
    if ($LASTEXITCODE -ne 0) { Fail 'could not install the build dependencies.' }
    Write-Ok 'setuptools, wheel, Cython, cysignals'

    Write-Step 'Compiling tesserocr'
    uv pip install --python $venvPython --no-build-isolation --no-cache tesserocr
    if ($LASTEXITCODE -ne 0) {
        Fail @"
tesserocr failed to compile.

Check the error above against these usual causes:
  * "Tesseract library not found in LIBPATH" -> LIBPATH did not survive; it is
    currently: $env:LIBPATH
  * a Cython or 'cythonize' error -> build isolation crept back in
  * C1083 cannot open include file -> INCLUDE was replaced instead of appended
"@
    }
    Write-Ok 'tesserocr compiled'

    # ----------------------------------------------------------------------
    # 9. Runtime DLLs
    # ----------------------------------------------------------------------
    Write-Step 'Placing the runtime DLLs'
    # Since Python 3.8, PATH is not searched for extension-module dependencies,
    # so the DLLs must sit beside the .pyd (or be added via os.add_dll_directory).
    $sitePkgs = & $venvPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    $target   = Join-Path $sitePkgs 'tesserocr'
    if (-not (Test-Path $target)) { $target = $sitePkgs }
    $copied = 0
    Get-ChildItem -Path $binDir -Filter '*.dll' -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName -Destination $target -Force
        $copied++
    }
    Write-Ok "copied $copied DLL(s) into $target"

    # ----------------------------------------------------------------------
    # 10. Language data
    # ----------------------------------------------------------------------
    Write-Step 'Locating tessdata'
    if (-not $TessdataPrefix) {
        $candidates = @(
            (Join-Path $installed 'share\tessdata'),
            'C:\Program Files\Tesseract-OCR\tessdata',
            'C:\Program Files (x86)\Tesseract-OCR\tessdata',
            $env:TESSDATA_PREFIX
        ) | Where-Object { $_ }
        foreach ($c in $candidates) {
            if (Test-Path (Join-Path $c 'eng.traineddata')) { $TessdataPrefix = $c; break }
        }
    }
    if (-not $TessdataPrefix) {
        $TessdataPrefix = Join-Path $installed 'share\tessdata'
        New-Item -ItemType Directory -Force -Path $TessdataPrefix | Out-Null
        Write-Host '    downloading eng.traineddata...'
        try {
            Invoke-WebRequest -UseBasicParsing `
                -Uri 'https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata' `
                -OutFile (Join-Path $TessdataPrefix 'eng.traineddata')
        } catch {
            Fail @"
No eng.traineddata found and the download failed.
Download it manually into $TessdataPrefix :
  https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
"@
        }
    }
    $env:TESSDATA_PREFIX = $TessdataPrefix
    [Environment]::SetEnvironmentVariable('TESSDATA_PREFIX', $TessdataPrefix, 'User')
    Write-Ok "TESSDATA_PREFIX = $TessdataPrefix (also set for your user account)"

    # ----------------------------------------------------------------------
    # 11. The rest of the requirements
    # ----------------------------------------------------------------------
    Write-Step 'Installing the remaining dependencies'
    uv pip install --python $venvPython -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail 'could not install requirements.txt' }
    Write-Ok 'requirements.txt installed'

    $wc = (& $venvPython -c "import importlib.metadata as m; print(m.version('windows-capture'))" 2>$null)
    if ($wc -and $wc -notlike '1.*') {
        Write-Warn2 "windows-capture $wc installed, but capture.py was written against the 1.x API."
        Write-Warn2 "If 'run' fails inside capture.py, pin it: uv pip install 'windows-capture<2'"
    }

    # ----------------------------------------------------------------------
    # 12. Verify
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
        "could not initialise Tesseract ({}). The compile worked, but the "
        "language data was not found: point TESSDATA_PREFIX at a directory "
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
        & $venvPython $checkPy $TessdataPrefix
        $checkExit = $LASTEXITCODE
    } finally {
        Remove-Item $checkPy -Force -ErrorAction SilentlyContinue
    }
    if ($checkExit -ne 0) { Fail 'tesserocr imported or ran incorrectly. See the error above.' }
    Write-Ok 'tesserocr works'

    Write-Step 'Running the offline test suite'
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { Write-Warn2 'some tests failed -- see output above' } else { Write-Ok 'tests passed' }
}
finally { Pop-Location }

# --------------------------------------------------------------------------
# 13. The X-Plane side
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

   .venv\Scripts\activate
   python -m g1000_softkey.main list-windows
   python -m g1000_softkey.main calibrate --display pfd

$(if ($simInstalled) {
"  X-Plane side installed. Start X-Plane, then confirm the
  datarefs registered:
    scripts\install-xplane-plugin.ps1 -VerifyOnly"
} else {
"  X-Plane side NOT installed. Run:
    scripts\install-xplane-plugin.ps1"
})

 Note: LIBPATH/INCLUDE were set for THIS shell only -- they are
 build-time settings and are not needed to run the daemon.
============================================================
"@ -ForegroundColor Green
