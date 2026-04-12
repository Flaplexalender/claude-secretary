# Paid-Billing Optimization Launch Script
# Launches 3 Secretary instances (researcher, builder, tester) focused on
# reducing premium request costs when billing is ON.
#
# Usage:
#   .\scripts\optimize-paid-billing.ps1
#   .\scripts\optimize-paid-billing.ps1 -MaxRuns 2

param(
    [int]$MaxRuns = 1
)

$python = ".venv\Scripts\python.exe"
$campaign = "campaigns\optimize-paid-billing.yaml"

Write-Host "`n=== Paid-Billing Optimization ===" -ForegroundColor Cyan
Write-Host "Campaign: $campaign"
Write-Host "Max runs per instance: $MaxRuns"
Write-Host "Focus: Reduce turns-per-task and optimize tier routing`n"

# Ensure shared directories exist
New-Item -ItemType Directory -Force -Path "data\shared\metrics", "data\shared\claims", "data\shared\results" | Out-Null
foreach ($role in @("researcher", "builder", "tester")) {
    New-Item -ItemType Directory -Force -Path "data\$role" | Out-Null
}

# Initialize scratchpad
if (-not (Test-Path "data\scratchpad.md")) {
    "# Paid-Billing Optimization Scratchpad`n`nShared across instances.`n" | Set-Content "data\scratchpad.md"
}

Write-Host "Launching 3 instances..." -ForegroundColor Yellow

$researcher = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "researcher", "--role", "researcher", "--coordinate",
    "--campaign", $campaign, "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\researcher\stdout.log" -RedirectStandardError "data\researcher\stderr.log"
Write-Host "  Researcher (PID $($researcher.Id))" -ForegroundColor Green

Start-Sleep -Seconds 2

$builder = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "builder", "--role", "builder", "--coordinate",
    "--campaign", $campaign, "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\builder\stdout.log" -RedirectStandardError "data\builder\stderr.log"
Write-Host "  Builder (PID $($builder.Id))" -ForegroundColor Green

Start-Sleep -Seconds 2

$tester = Start-Process -FilePath $python -ArgumentList @(
    "-m", "secretary", "watch",
    "--instance", "tester", "--role", "tester", "--coordinate",
    "--campaign", $campaign, "--max-runs", $MaxRuns
) -PassThru -NoNewWindow -RedirectStandardOutput "data\tester\stdout.log" -RedirectStandardError "data\tester\stderr.log"
Write-Host "  Tester (PID $($tester.Id))" -ForegroundColor Green

Write-Host "`nAll 3 instances launched. Monitor:" -ForegroundColor Cyan
Write-Host "  $python -m secretary metrics instances"
Write-Host "  $python -m secretary metrics show"
Write-Host "  Get-Content data\researcher\stderr.log -Tail 20"

Write-Host "`nWaiting for all instances..." -ForegroundColor Yellow

$researcher.WaitForExit()
Write-Host "  Researcher done (exit: $($researcher.ExitCode))" -ForegroundColor $(if ($researcher.ExitCode -eq 0) { "Green" } else { "Red" })

$builder.WaitForExit()
Write-Host "  Builder done (exit: $($builder.ExitCode))" -ForegroundColor $(if ($builder.ExitCode -eq 0) { "Green" } else { "Red" })

$tester.WaitForExit()
Write-Host "  Tester done (exit: $($tester.ExitCode))" -ForegroundColor $(if ($tester.ExitCode -eq 0) { "Green" } else { "Red" })

Write-Host "`n=== Results ===" -ForegroundColor Cyan
& $python -m secretary metrics show 2>&1
Write-Host "`n=== Scratchpad ===" -ForegroundColor Cyan
if (Test-Path "data\scratchpad.md") {
    Get-Content "data\scratchpad.md" | Select-Object -First 100
}
