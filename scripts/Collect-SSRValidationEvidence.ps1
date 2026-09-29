param(
    [string]$RepoPath = "C:\Streaming\StreamStateRouter\Source",
    [string]$OutputRoot = "$env:USERPROFILE\Desktop\SSR-Validation",
    [switch]$SkipObsInspection
)

$ErrorActionPreference = "Stop"

function Write-TextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Lines
    )
    $Lines | Set-Content -LiteralPath $Path -Encoding UTF8
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$destination = Join-Path $OutputRoot "SSR-$stamp"
New-Item -ItemType Directory -Path $destination -Force | Out-Null

$summary = @(
    "CollectedAt=$(Get-Date -Format o)"
    "Computer=$env:COMPUTERNAME"
    "User=$env:USERNAME"
    "RepoPath=$RepoPath"
)

if (Test-Path -LiteralPath (Join-Path $RepoPath ".git")) {
    try {
        $head = (& git -C $RepoPath rev-parse HEAD 2>&1 | Out-String).Trim()
        $branch = (& git -C $RepoPath branch --show-current 2>&1 | Out-String).Trim()
        $status = (& git -C $RepoPath status --short 2>&1 | Out-String).TrimEnd()
        $summary += "GitHead=$head"
        $summary += "GitBranch=$branch"
        Write-TextFile -Path (Join-Path $destination "git-status.txt") -Lines @(
            "HEAD: $head"
            "Branch: $branch"
            ""
            "git status --short:"
            $status
        )
    }
    catch {
        $summary += "GitError=$($_.Exception.Message)"
    }
}
else {
    $summary += "GitRepo=not-found"
}

$runtimePath = Join-Path $env:APPDATA "StreamStateRouter\runtime.json"
if (Test-Path -LiteralPath $runtimePath) {
    Copy-Item -LiteralPath $runtimePath -Destination (Join-Path $destination "runtime.json") -Force
    $summary += "RuntimeMarker=copied"
}
else {
    $summary += "RuntimeMarker=not-found"
}

$ssrLogs = Join-Path $env:APPDATA "StreamStateRouter\logs"
if (Test-Path -LiteralPath $ssrLogs) {
    $latestSsrLogs = Get-ChildItem -LiteralPath $ssrLogs -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 5
    if ($latestSsrLogs) {
        $target = Join-Path $destination "ssr-logs"
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($item in $latestSsrLogs) {
            Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $target $item.Name) -Force
        }
        $summary += "SsrLogs=$($latestSsrLogs.Count)"
    }
}

$obsLogs = Join-Path $env:APPDATA "obs-studio\logs"
if (Test-Path -LiteralPath $obsLogs) {
    $latestObs = Get-ChildItem -LiteralPath $obsLogs -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 2
    if ($latestObs) {
        $target = Join-Path $destination "obs-logs"
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($item in $latestObs) {
            Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $target $item.Name) -Force
        }
        $summary += "ObsLogs=$($latestObs.Count)"
    }
}

if (-not $SkipObsInspection) {
    $inspectionScript = Join-Path $RepoPath "scripts\inspect_layout_fade_state.py"
    if (Test-Path -LiteralPath $inspectionScript) {
        $pythonCandidates = @(
            (Join-Path $RepoPath ".venv\Scripts\python.exe"),
            "python",
            "py"
        )
        $python = $null
        foreach ($candidate in $pythonCandidates) {
            if ([System.IO.Path]::IsPathRooted($candidate)) {
                if (Test-Path -LiteralPath $candidate) {
                    $python = $candidate
                    break
                }
                continue
            }
            if (Get-Command $candidate -ErrorAction SilentlyContinue) {
                $python = $candidate
                break
            }
        }

        if ($python) {
            $report = Join-Path $destination "fade-inspection.json"
            try {
                if ($python -eq "py") {
                    & $python -3 $inspectionScript --report $report
                }
                else {
                    & $python $inspectionScript --report $report
                }
                $summary += "FadeInspectionExitCode=$LASTEXITCODE"
            }
            catch {
                $summary += "FadeInspectionError=$($_.Exception.Message)"
            }
        }
        else {
            $summary += "FadeInspection=python-not-found"
        }
    }
    else {
        $summary += "FadeInspection=script-not-found"
    }
}

try {
    $processes = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match "^(obs64|StreamStateRouter|python|pythonw)\.exe$"
        } |
        Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine

    $processes |
        Format-List |
        Out-String |
        Set-Content -LiteralPath (Join-Path $destination "processes.txt") -Encoding UTF8
}
catch {
    $summary += "ProcessInventoryError=$($_.Exception.Message)"
}

Write-TextFile -Path (Join-Path $destination "summary.txt") -Lines $summary

Write-Host ""
Write-Host "Preuves SSR collectées dans :" -ForegroundColor Green
Write-Host $destination
Write-Host ""
Write-Host "Ce script ne modifie ni OBS ni la configuration SSR."
