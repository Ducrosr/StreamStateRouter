param(
    [string]$RepoPath = "C:\Streaming\StreamStateRouter\Source",
    [string]$OutputRoot = "$env:USERPROFILE\Desktop\SSR-Validation",
    [switch]$SkipObsInspection,
    [switch]$RawLocal,
    [int64]$MaxRawLogBytes = 4194304
)

$ErrorActionPreference = "Stop"
$script:Partial = $false

function Write-TextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Lines
    )
    $Lines | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Mark-Partial {
    param([Parameter(Mandatory = $true)][string]$Message)
    $script:Partial = $true
    $script:Summary += $Message
}

function Invoke-GitText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & git @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "git exited with code $exitCode"
    }
    return ($output | Out-String).Trim()
}

function New-UniqueDestination {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$BaseName
    )

    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    $candidate = Join-Path $Root $BaseName
    $index = 2
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $Root "$BaseName-$index"
        $index += 1
    }
    New-Item -ItemType Directory -Path $candidate | Out-Null
    return $candidate
}

function Copy-RawLogsBounded {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][int]$MaxCount,
        [Parameter(Mandatory = $true)][int64]$MaxBytes
    )

    if (-not (Test-Path -LiteralPath $Source)) {
        return [pscustomobject]@{ Count = 0; Bytes = 0; Truncated = $false }
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $count = 0
    $bytes = [int64]0
    $truncated = $false

    $items = Get-ChildItem -LiteralPath $Source -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First $MaxCount

    foreach ($item in $items) {
        if (($bytes + [int64]$item.Length) -gt $MaxBytes) {
            $truncated = $true
            continue
        }
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $Destination $item.Name)
        $count += 1
        $bytes += [int64]$item.Length
    }

    return [pscustomobject]@{
        Count = $count
        Bytes = $bytes
        Truncated = $truncated
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$destination = New-UniqueDestination -Root $OutputRoot -BaseName "SSR-$stamp"

$script:Summary = @(
    "CollectedAt=$(Get-Date -Format o)"
    "Mode=$(if ($RawLocal) { 'raw-local-sensitive' } else { 'shareable-redacted' })"
    "DestinationId=$(Split-Path -Leaf $destination)"
)

if ($RawLocal) {
    $script:Summary += "WARNING=RawLocal contains sensitive local paths/logs/process command lines; do not share without review."
}

try {
    if (Test-Path -LiteralPath (Join-Path $RepoPath ".git")) {
        $head = Invoke-GitText -Arguments @("-C", $RepoPath, "rev-parse", "HEAD")
        $branchName = Invoke-GitText -Arguments @("-C", $RepoPath, "branch", "--show-current")
        $status = Invoke-GitText -Arguments @("-C", $RepoPath, "status", "--short")
        $script:Summary += "SourceGitHead=$head"
        $script:Summary += "SourceGitBranch=$branchName"

        Write-TextFile -Path (Join-Path $destination "git-status.txt") -Lines @(
            "HEAD: $head"
            "Branch: $branchName"
            ""
            "git status --short:"
            $status
        )
    }
    else {
        Mark-Partial "GitRepo=not-found"
    }
}
catch {
    Mark-Partial "GitError=$($_.Exception.GetType().Name)"
}

$runtimePath = Join-Path $env:APPDATA "StreamStateRouter\runtime.json"
try {
    if (Test-Path -LiteralPath $runtimePath) {
        if ($RawLocal) {
            Copy-Item -LiteralPath $runtimePath -Destination (Join-Path $destination "runtime.raw.json")
            $script:Summary += "RuntimeMarker=raw-copied"
        }
        else {
            $runtimeRaw = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
            $pending = @()
            foreach ($item in @($runtimeRaw.pending_cleanup)) {
                if ($null -eq $item) {
                    continue
                }
                $pending += [ordered]@{
                    kind = $item.kind
                    source_kind = $item.source_kind
                    filter_kind = $item.filter_kind
                    cleanup_action = $item.cleanup_action
                    legacy = [bool]$item.legacy
                    ambiguous = [bool]$item.ambiguous
                    attempts = $item.attempts
                    has_source_identity = (
                        -not [string]::IsNullOrWhiteSpace([string]$item.source) -or
                        -not [string]::IsNullOrWhiteSpace([string]$item.source_uuid)
                    )
                    has_helper_identity = (
                        -not [string]::IsNullOrWhiteSpace([string]$item.helper_id) -and
                        -not [string]::IsNullOrWhiteSpace([string]$item.filter_name)
                    )
                    has_error = -not [string]::IsNullOrWhiteSpace([string]$item.last_error)
                }
            }
            $runtimeSafe = [ordered]@{
                clean_shutdown = $runtimeRaw.clean_shutdown
                cleanup_complete = $runtimeRaw.cleanup_complete
                cleanup_schema = $runtimeRaw.cleanup_schema
                pending_cleanup = $pending
            }
            $runtimeSafe |
                ConvertTo-Json -Depth 8 |
                Set-Content -LiteralPath (Join-Path $destination "runtime-summary.json") -Encoding UTF8
            $script:Summary += "RuntimeMarker=redacted-summary"
        }
    }
    else {
        $script:Summary += "RuntimeMarker=not-found"
    }
}
catch {
    Mark-Partial "RuntimeMarkerError=$($_.Exception.GetType().Name)"
}

if ($RawLocal) {
    try {
        $ssrResult = Copy-RawLogsBounded -Source (Join-Path $env:APPDATA "StreamStateRouter\logs") -Destination (Join-Path $destination "ssr-logs-raw") -MaxCount 5 -MaxBytes $MaxRawLogBytes
        $script:Summary += "SsrLogsRawCount=$($ssrResult.Count)"
        $script:Summary += "SsrLogsRawBytes=$($ssrResult.Bytes)"
        $script:Summary += "SsrLogsRawTruncated=$($ssrResult.Truncated)"
    }
    catch {
        Mark-Partial "SsrLogsError=$($_.Exception.GetType().Name)"
    }

    try {
        $obsResult = Copy-RawLogsBounded -Source (Join-Path $env:APPDATA "obs-studio\logs") -Destination (Join-Path $destination "obs-logs-raw") -MaxCount 2 -MaxBytes $MaxRawLogBytes
        $script:Summary += "ObsLogsRawCount=$($obsResult.Count)"
        $script:Summary += "ObsLogsRawBytes=$($obsResult.Bytes)"
        $script:Summary += "ObsLogsRawTruncated=$($obsResult.Truncated)"
    }
    catch {
        Mark-Partial "ObsLogsError=$($_.Exception.GetType().Name)"
    }
}
else {
    $script:Summary += "RawLogs=omitted"
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
                $inspectionArgs = @($inspectionScript, "--report", $report)
                if ($RawLocal) {
                    $inspectionArgs += "--raw-local"
                }
                if ($python -eq "py") {
                    & $python -3 @inspectionArgs
                }
                else {
                    & $python @inspectionArgs
                }
                $inspectionExitCode = $LASTEXITCODE
                $script:Summary += "FadeInspectionExitCode=$inspectionExitCode"
                if ($inspectionExitCode -ne 0) {
                    Mark-Partial "FadeInspection=partial-or-error"
                }
            }
            catch {
                Mark-Partial "FadeInspectionError=$($_.Exception.GetType().Name)"
            }
        }
        else {
            Mark-Partial "FadeInspection=python-not-found"
        }
    }
    else {
        Mark-Partial "FadeInspection=script-not-found"
    }
}
else {
    $script:Summary += "FadeInspection=skipped"
}

try {
    if ($RawLocal) {
        $processes = Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -match "^(obs64|StreamStateRouter|python|pythonw)\.exe$"
            } |
            Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
    }
    else {
        $processes = Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -match "^(obs64|StreamStateRouter)\.exe$"
            } |
            Select-Object ProcessId, ParentProcessId, Name
    }

    $processes |
        Format-List |
        Out-String |
        Set-Content -LiteralPath (Join-Path $destination "processes.txt") -Encoding UTF8

    $runningSsr = Get-Process -Name "StreamStateRouter" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $runningSsr) {
        $script:Summary += "RunningSSRProcessId=$($runningSsr.Id)"
        try {
            $runningPath = $runningSsr.Path
            if (-not [string]::IsNullOrWhiteSpace($runningPath)) {
                $versionInfo = (Get-Item -LiteralPath $runningPath).VersionInfo
                $script:Summary += "RunningSSRFileVersion=$($versionInfo.FileVersion)"
                $script:Summary += "RunningSSRProductVersion=$($versionInfo.ProductVersion)"
                if ($RawLocal) {
                    $script:Summary += "RunningSSRPath=$runningPath"
                }
            }
        }
        catch {
            Mark-Partial "RunningSSRVersionError=$($_.Exception.GetType().Name)"
        }
    }
    else {
        $script:Summary += "RunningSSR=not-detected-as-exe"
    }
}
catch {
    Mark-Partial "ProcessInventoryError=$($_.Exception.GetType().Name)"
}

$overallStatus = if ($script:Partial) { "partial" } else { "ok" }
$script:Summary = @("Status=$overallStatus") + $script:Summary
Write-TextFile -Path (Join-Path $destination "summary.txt") -Lines $script:Summary

Write-Host ""
if ($script:Partial) {
    Write-Host "Collecte SSR terminée avec éléments partiels :" -ForegroundColor Yellow
}
else {
    Write-Host "Collecte SSR terminée :" -ForegroundColor Green
}
Write-Host $destination
Write-Host ""
if ($RawLocal) {
    Write-Host "ATTENTION : ce dossier contient des données brutes potentiellement sensibles." -ForegroundColor Yellow
}
else {
    Write-Host "Mode partageable : logs bruts, chemins personnels, command lines et identifiants OBS détaillés omis."
}
Write-Host "Le collecteur ne modifie ni OBS ni la configuration SSR ; il écrit uniquement ses artefacts de validation."

if ($script:Partial) {
    exit 2
}
exit 0
