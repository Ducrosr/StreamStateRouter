$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    Write-Host "Python 3.12+ est requis pour cette archive source." -ForegroundColor Red
    Write-Host "La release GitHub compilée n'aura pas cette dépendance." -ForegroundColor Yellow
    exit 1
}

$VersionText = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
$Parts = $VersionText.Split('.')
if ([int]$Parts[0] -lt 3 -or ([int]$Parts[0] -eq 3 -and [int]$Parts[1] -lt 12)) {
    Write-Host "Python $VersionText détecté ; Python 3.12+ est requis." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
& .\.venv\Scripts\python.exe main.py
