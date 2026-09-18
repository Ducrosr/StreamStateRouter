$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .venv)) {
    py -3.12 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[all]"

Write-Host "=== Python : tests + lint ===" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe main.py --check-config

Write-Host "=== Application Windows ===" -ForegroundColor Cyan
& .\.venv\Scripts\pyinstaller.exe --clean --noconfirm StreamStateRouter.spec

$Version = (& .\.venv\Scripts\python.exe -c "import stream_state_router; print(stream_state_router.__version__)").Trim()
$Release = Join-Path $PWD "release"
New-Item -ItemType Directory -Force -Path $Release | Out-Null
$Portable = Join-Path $Release "Stream-State-Router-v$Version-portable.zip"
if (Test-Path $Portable) { Remove-Item $Portable -Force }
Compress-Archive -Path .\dist\StreamStateRouter.exe -DestinationPath $Portable

Write-Host "=== Plugin Stream Deck ===" -ForegroundColor Cyan
$Npm = Get-Command npm -ErrorAction SilentlyContinue
if ($Npm) {
    Push-Location .\streamdeck-plugin
    try {
        npm install
        npm run typecheck
        npm run build
        npm run validate
        npm run pack
        $Plugin = Get-ChildItem -Filter *.streamDeckPlugin | Select-Object -First 1
        if ($Plugin) {
            Copy-Item $Plugin.FullName (Join-Path $Release $Plugin.Name) -Force
        }
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
    & $Iscc.Source ".\installer\StreamStateRouter.iss"
} else {
    Write-Host "Inno Setup absent : portable créé, installateur ignoré." -ForegroundColor Yellow
}

Write-Host "Build terminé : $Portable" -ForegroundColor Green
Write-Host "Artefacts : $Release" -ForegroundColor Green
