$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Commande native échouée ($LASTEXITCODE) : $FilePath $($Arguments -join ' ')"
    }
}

function Get-PythonVersionText {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath
    )

    $Version = (& $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Version) {
        throw "Impossible de déterminer la version Python : $PythonPath"
    }
    return $Version
}

function Assert-Python312 {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $VersionText = Get-PythonVersionText $PythonPath
    $Parts = $VersionText.Split('.')
    if (
        [int]$Parts[0] -lt 3 -or
        ([int]$Parts[0] -eq 3 -and [int]$Parts[1] -lt 12)
    ) {
        throw "$Label utilise Python $VersionText ; Python 3.12+ est requis."
    }
    Write-Host "$Label : Python $VersionText" -ForegroundColor DarkGray
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    Write-Host "Python 3.12+ est requis pour cette archive source." -ForegroundColor Red
    Write-Host "La release GitHub compilée n'aura pas cette dépendance." -ForegroundColor Yellow
    exit 1
}

$SystemPython = $PythonCommand.Source
Assert-Python312 $SystemPython "Python système"

if (-not (Test-Path .venv)) {
    Invoke-Native $SystemPython -m venv .venv
}

$VenvPythonPath = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPythonPath)) {
    throw "Le dossier .venv existe mais son interpréteur est introuvable. Supprimez .venv puis relancez ce script."
}

$VenvPython = (Resolve-Path $VenvPythonPath).Path
try {
    Assert-Python312 $VenvPython "Environnement .venv"
}
catch {
    throw "$($_.Exception.Message) Supprimez .venv puis relancez ce script pour le recréer."
}

Invoke-Native $VenvPython -m pip install --upgrade pip
Invoke-Native $VenvPython -m pip install -e ".[desktop]"
Invoke-Native $VenvPython main.py
