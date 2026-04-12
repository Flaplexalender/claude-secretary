# run-tests-safe.ps1 — Run secretary tests without crashing VS Code
# Closes VS Code, runs pytest, shows results, reopens VS Code.

param(
    [string]$Filter = "",
    [switch]$KeepClosed
)

$secretaryDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = "$secretaryDir\.venv\Scripts\Activate.ps1"
$vscodeExe = "code-insiders"

# Check for running VS Code
$vscodeProcs = Get-Process -Name "Code - Insiders" -ErrorAction SilentlyContinue

if ($vscodeProcs) {
    Write-Host "Closing VS Code Insiders..." -ForegroundColor Yellow
    $vscodeProcs | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 3

    # Force kill if still running
    $remaining = Get-Process -Name "Code - Insiders" -ErrorAction SilentlyContinue
    if ($remaining) {
        Write-Host "Force-closing remaining VS Code processes..." -ForegroundColor Red
        $remaining | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    Write-Host "VS Code closed." -ForegroundColor Green
} else {
    Write-Host "VS Code not running." -ForegroundColor Green
}

# Activate venv and run tests
Push-Location $secretaryDir
try {
    & $venv

    $pytestArgs = @("-v", "--tb=short", "-q")
    if ($Filter) {
        $pytestArgs += @("-k", $Filter)
    }

    Write-Host "`nRunning pytest..." -ForegroundColor Cyan
    python -m pytest @pytestArgs
    $testExitCode = $LASTEXITCODE

    if ($testExitCode -eq 0) {
        Write-Host "`nAll tests passed." -ForegroundColor Green
    } else {
        Write-Host "`nTests failed (exit code $testExitCode)." -ForegroundColor Red
    }
} finally {
    Pop-Location
}

# Reopen VS Code unless told not to
if (-not $KeepClosed) {
    Write-Host "`nReopening VS Code..." -ForegroundColor Yellow
    Start-Process $vscodeExe
}

exit $testExitCode
