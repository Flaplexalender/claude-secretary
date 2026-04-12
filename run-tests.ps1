# run-tests.ps1 — Close VS Code, run tests, reopen VS Code
#
# WHY: Running pytest while VS Code is open crashes VS Code due to an Electron 39
# bug where the Chromium network service dies and can't recover (cascade crash).
# Tests themselves are fine — they just generate filesystem activity that stresses
# VS Code's internal network service.
#
# WHEN TO REMOVE: After VS Code updates to Electron 42+ (~July 2026, VS Code 1.117+).
# Electron 42 adds network service crash recovery (PR #49887).
# Check your Electron version: Help > About in VS Code.
# If Electron >= 42.x.x, this workaround is no longer needed.
#
# Tracking: https://github.com/electron/electron/issues/49572

param(
    [switch]$SkipReopen,
    [string]$TestArgs = "tests/ -x --tb=short"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Secretary Test Runner ===" -ForegroundColor Cyan

# Step 1: Close VS Code if running
$vscode = Get-Process -Name "Code" -ErrorAction SilentlyContinue
if ($vscode) {
    Write-Host "Closing VS Code ($($vscode.Count) processes)..." -ForegroundColor Yellow
    $vscode | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 3
    $remaining = Get-Process -Name "Code" -ErrorAction SilentlyContinue
    if ($remaining) {
        $remaining | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    $still = Get-Process -Name "Code" -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "ERROR: Could not close VS Code." -ForegroundColor Red
        exit 1
    }
    Write-Host "VS Code closed." -ForegroundColor Green
} else {
    Write-Host "VS Code not running." -ForegroundColor Green
}

# Step 2: Activate venv and run tests
Push-Location $scriptDir
$venvActivate = Join-Path $scriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
}

Write-Host "Running: python -m pytest $TestArgs" -ForegroundColor Cyan
$testStart = Get-Date
Invoke-Expression "python -m pytest $TestArgs"
$testExit = $LASTEXITCODE
$duration = [Math]::Round(((Get-Date) - $testStart).TotalSeconds)
Pop-Location

Write-Host ""
if ($testExit -eq 0) {
    Write-Host "PASSED ($duration s)" -ForegroundColor Green
} else {
    Write-Host "FAILED (exit $testExit, $duration s)" -ForegroundColor Red
}

# Step 3: Reopen VS Code
if (-not $SkipReopen) {
    $codeExe = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"
    if (Test-Path $codeExe) {
        Write-Host "Reopening VS Code..." -ForegroundColor Yellow
        Start-Process $codeExe
        Write-Host "Done." -ForegroundColor Green
    }
}

exit $testExit
