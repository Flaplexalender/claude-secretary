# Self-Optimization Launch Script
# Launches 3 Secretary instances (researcher, builder, tester) to optimize Secretary itself.
# All instances use Opus via proxy with extended thinking.
#
# Usage:
#   .\scripts\self-optimize.ps1
#   .\scripts\self-optimize.ps1 -MaxRuns 2     # multiple cycles
#
# Monitor:
#   .venv\Scripts\python.exe -m secretary metrics instances
#   .venv\Scripts\python.exe -m secretary metrics show
#   Get-Content data\shared\metrics\task_metrics.jsonl | Select-Object -Last 5

param(
    [int]$MaxRuns = 1
)

$python = ".venv\Scripts\python.exe"
$campaign = "campaigns\self-optimize-opus.yaml"

Write-Host "`n=== Secretary Self-Optimization ===" -ForegroundColor Cyan
Write-Host "Campaign: $campaign"
Write-Host "Max runs per instance: $MaxRuns"
Write-Host "All instances use Opus 4.6 via proxy`n"

# Ensure shared directories exist
New-Item -ItemType Directory -Force -Path "data\shared\metrics" | Out-Null
New-Item -ItemType Directory -Force -Path "data\shared\claims" | Out-Null
New-Item -ItemType Directory -Force -Path "data\shared\results" | Out-Null

# Initialize scratchpad if it doesn't exist
if (-not (Test-Path "data\scratchpad.md")) {
    "# Secretary Self-Optimization Scratchpad`n`nThis file is shared across Secretary instances for cross-cycle research notes.`n" | Set-Content "data\scratchpad.md"
}

Write-Host "Launching 3 instances..." -ForegroundColor Yellow

# Instance 1: Researcher
$researcher = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "researcher",
    "--role", "researcher",
    "--coordinate",
    "--campaign", $campaign,
    "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\researcher\stdout.log" -RedirectStandardError "data\researcher\stderr.log"
Write-Host "  Researcher (PID $($researcher.Id)) started" -ForegroundColor Green

Start-Sleep -Seconds 2

# Instance 2: Builder (waits for researcher via depends_on)
$builder = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "builder",
    "--role", "builder",
    "--coordinate",
    "--campaign", $campaign,
    "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\builder\stdout.log" -RedirectStandardError "data\builder\stderr.log"
Write-Host "  Builder (PID $($builder.Id)) started" -ForegroundColor Green

Start-Sleep -Seconds 2

# Instance 3: Tester (waits for builder via depends_on)
$tester = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "tester",
    "--role", "tester",
    "--coordinate",
    "--campaign", $campaign,
    "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\tester\stdout.log" -RedirectStandardError "data\tester\stderr.log"
Write-Host "  Tester (PID $($tester.Id)) started" -ForegroundColor Green

Write-Host "`nAll 3 instances launched. Monitor with:" -ForegroundColor Cyan
Write-Host "  $python -m secretary metrics instances"
Write-Host "  $python -m secretary metrics show"
Write-Host "  Get-Content data\researcher\stderr.log -Tail 20"
Write-Host "  Get-Content data\builder\stderr.log -Tail 20"
Write-Host "  Get-Content data\tester\stderr.log -Tail 20"

Write-Host "`nWaiting for all instances to complete..." -ForegroundColor Yellow

# Wait for all processes
$researcher.WaitForExit()
Write-Host "  Researcher finished (exit: $($researcher.ExitCode))" -ForegroundColor $(if ($researcher.ExitCode -eq 0) { "Green" } else { "Red" })

$builder.WaitForExit()
Write-Host "  Builder finished (exit: $($builder.ExitCode))" -ForegroundColor $(if ($builder.ExitCode -eq 0) { "Green" } else { "Red" })

$tester.WaitForExit()
Write-Host "  Tester finished (exit: $($tester.ExitCode))" -ForegroundColor $(if ($tester.ExitCode -eq 0) { "Green" } else { "Red" })

Write-Host "`n=== Results ===" -ForegroundColor Cyan
& $python -m secretary metrics show 2>&1
Write-Host "`n=== Scratchpad (findings) ===" -ForegroundColor Cyan
if (Test-Path "data\scratchpad.md") {
    Get-Content "data\scratchpad.md" | Select-Object -First 100
}
