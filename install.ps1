# lanecast installer for Windows. Paste this into PowerShell:
#
#   irm https://raw.githubusercontent.com/aaronhhsi/lanecast/main/install.ps1 | iex
#
# or double-click install.cmd in a downloaded copy. Everything goes in one folder,
# %LOCALAPPDATA%\lanecast (app\, env\, bin\, data\), and lanecast starts at login.
# Windows may ask you to allow an install (Python, Git) or show a firewall prompt
# for laneq: both are fine to allow. Running it again updates an install.
#
# For testing: LANECAST_DIR (the folder), LANECAST_SOURCE (copy the code from this
# folder instead of cloning), LANECAST_LANEQ_BIN (use this laneq binary).
$ErrorActionPreference = "Stop"
$Root = if ($env:LANECAST_DIR) { $env:LANECAST_DIR } else { Join-Path $env:LOCALAPPDATA "lanecast" }
$Repo = if ($env:LANECAST_REPO) { $env:LANECAST_REPO } else { "https://github.com/aaronhhsi/lanecast.git" }

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
    # "python" can be the Microsoft Store's placeholder, which opens the Store instead of running: check the version it reports.
    foreach ($cmd in @(@("py", "-3"), @("python"), @("python3"))) {
        try {
            $exe = $cmd[0]; $rest = @($cmd | Select-Object -Skip 1)
            $out = & $exe @rest -c "import sys; print(sys.version_info >= (3, 10), sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match "^True (.+)$") { return $Matches[1].Trim() }
        } catch {}
    }
    return $null
}
function Need-Winget($what) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$what isn't installed, and this PC has no winget to install it with. Install $what yourself, then run this again."
    }
}

Write-Host "Installing lanecast into $Root" -ForegroundColor Green

Step "Python"
$py = Find-Python
if (-not $py) {
    Need-Winget "Python"
    Write-Host "Installing Python 3.12 (Windows may ask you to allow it)..."
    winget install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    Refresh-Path
    $py = Find-Python
    if (-not $py) { $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe" }
    if (-not (Test-Path $py)) { throw "Python didn't install; install it from python.org (tick 'Add to PATH'), then run this again." }
}
Write-Host "Using $py"

Step "Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Need-Winget "Git"
    Write-Host "Installing Git (Windows may ask you to allow it)..."
    winget install --exact --id Git.Git --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    Refresh-Path
}

Step "The GitHub CLI (to make the outbox repo for you)"
if (-not (Get-Command gh -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    winget install --exact --id GitHub.cli --silent --accept-package-agreements --accept-source-agreements | Out-Host
    Refresh-Path
}

Step "lanecast's code"
New-Item -ItemType Directory -Force $Root | Out-Null
$App = Join-Path $Root "app"
if ($env:LANECAST_SOURCE) {
    if (Test-Path $App) { Remove-Item -Recurse -Force $App }
    robocopy $env:LANECAST_SOURCE $App /E /XD .git __pycache__ *.egg-info /NFL /NDL /NJH /NJS /NP | Out-Null
} elseif (Test-Path (Join-Path $App ".git")) {
    git -C $App pull --ff-only
} else {
    git clone --depth 1 $Repo $App
}

Step "Its Python packages (in its own environment)"
$VenvDir = Join-Path $Root "env"
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) { & $py -m venv $VenvDir }
$EnvPy = Join-Path $VenvDir "Scripts\python.exe"
& $EnvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $EnvPy -m pip install --quiet --disable-pip-version-check -e $App
if ($LASTEXITCODE -ne 0) { throw "Installing lanecast's packages failed (see above)." }

Step "laneq, start at login, and the first start"
$env:LANECAST_DIR = $Root
& $EnvPy -m lanecast install
if ($LASTEXITCODE -ne 0) { throw "The last step failed (see above)." }

Write-Host ""
Write-Host "Done. lanecast is in $Root" -ForegroundColor Green
Write-Host "Control it with: `"$Root\lanecast.cmd`" start | stop | status | update | uninstall"
