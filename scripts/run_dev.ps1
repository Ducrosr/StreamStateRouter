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

$Python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
Invoke-Native -FilePath $Python -ArgumentList @(
    "-m", "pip", "install", "-e", ".[desktop,dev]"
)
Invoke-Native -FilePath $Python -ArgumentList @("main.py")
