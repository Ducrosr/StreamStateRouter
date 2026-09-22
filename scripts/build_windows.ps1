$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$FilePath,
        [Parameter(Position = 1)][string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Commande native échouée ($LASTEXITCODE) : $FilePath $($ArgumentList -join ' ')"
    }
}

if (-not (Test-Path .venv)) {
    Invoke-Native -FilePath "py" -ArgumentList @("-3.12", "-m", "venv", ".venv")
}

$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
Invoke-Native -FilePath $Python -ArgumentList @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Native -FilePath $Python -ArgumentList @("-m", "pip", "install", "-e", ".[all]")

$Version = (& $Python -c "import stream_state_router; print(stream_state_router.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) { throw "Impossible de déterminer la version SSR." }

Write-Host "=== Python : tests + lint ===" -ForegroundColor Cyan
Invoke-Native -FilePath $Python -ArgumentList @("-m", "unittest", "discover", "-s", "tests", "-v")
Invoke-Native -FilePath $Python -ArgumentList @("-m", "ruff", "check", ".")
Invoke-Native -FilePath $Python -ArgumentList @("main.py", "--check-config")

Write-Host "=== Application Windows ===" -ForegroundColor Cyan
Invoke-Native -FilePath ".\.venv\Scripts\pyinstaller.exe" -ArgumentList @("--clean", "--noconfirm", "StreamStateRouter.spec")
if (-not (Test-Path .\dist\StreamStateRouter.exe)) { throw "EXE PyInstaller introuvable." }

$Smoke = Start-Process -FilePath .\dist\StreamStateRouter.exe -ArgumentList "--check-config" -Wait -PassThru
if ($Smoke.ExitCode -ne 0) { throw "Smoke test EXE échoué ($($Smoke.ExitCode))." }

$Release = Join-Path $PWD "release"
New-Item -ItemType Directory -Force -Path $Release | Out-Null
$Portable = Join-Path $Release "Stream-State-Router-v$Version-portable.zip"
if (Test-Path $Portable) { Remove-Item $Portable -Force }
Compress-Archive -Path .\dist\StreamStateRouter.exe -DestinationPath $Portable
if (-not (Test-Path $Portable)) { throw "Archive portable introuvable après création." }

Write-Host "=== Plugin Stream Deck ===" -ForegroundColor Cyan
$Npm = Get-Command npm -ErrorAction SilentlyContinue
if ($Npm) {
    Push-Location .\streamdeck-plugin
    try {
        Invoke-Native -FilePath $Npm.Source -ArgumentList @("ci", "--ignore-scripts", "--no-audit", "--no-fund")
        Invoke-Native -FilePath $Npm.Source -ArgumentList @("run", "typecheck")
        Invoke-Native -FilePath $Npm.Source -ArgumentList @("run", "build")
        Invoke-Native -FilePath $Npm.Source -ArgumentList @("run", "validate")
        Invoke-Native -FilePath $Npm.Source -ArgumentList @("run", "pack")
        $Plugin = Get-ChildItem -Filter *.streamDeckPlugin | Select-Object -First 1
        if (-not $Plugin) { throw "Artefact Stream Deck introuvable." }
        Copy-Item $Plugin.FullName (Join-Path $Release $Plugin.Name) -Force
    }
    finally {
        Pop-Location
    }
} else {
    Write-Host "Node/npm absent : plugin Stream Deck ignoré." -ForegroundColor Yellow
}

Write-Host "=== Installateur ===" -ForegroundColor Cyan
$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($Iscc) {
    Invoke-Native -FilePath $Iscc.Source -ArgumentList @("/DMyAppVersion=$Version", ".\installer\StreamStateRouter.iss")
    $Installer = Join-Path $Release "Stream-State-Router-v$Version-setup.exe"
    if (-not (Test-Path $Installer)) { throw "Installateur attendu introuvable : $Installer" }
} else {
    Write-Host "Inno Setup absent : portable créé, installateur ignoré." -ForegroundColor Yellow
}

$Commit = (& git rev-parse HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Commit) { $Commit = "unknown" }
$PythonVersion = (& $Python --version 2>&1).ToString().Trim()
$PyInstallerVersion = (& .\.venv\Scripts\pyinstaller.exe --version 2>&1).ToString().Trim()
$NodeVersion = if (Get-Command node -ErrorAction SilentlyContinue) { (& node --version 2>&1).ToString().Trim() } else { "not-installed" }
$NpmVersion = if (Get-Command npm -ErrorAction SilentlyContinue) { (& npm --version 2>&1).ToString().Trim() } else { "not-installed" }
$Manifest = [ordered]@{
    version = $Version
    commit = $Commit
    python = $PythonVersion
    pyinstaller = $PyInstallerVersion
    node = $NodeVersion
    npm = $NpmVersion
}
$Manifest | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Release "build-manifest.json")

Write-Host "Build terminé : $Portable" -ForegroundColor Green
Write-Host "Artefacts : $Release" -ForegroundColor Green
