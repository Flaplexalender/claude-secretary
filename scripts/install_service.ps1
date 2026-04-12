#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install claude-secretary as a Windows Service via NSSM.
.DESCRIPTION
    Idempotent — safe to re-run. Stops and removes existing service first.
    Requires NSSM: choco install nssm  OR  https://nssm.cc/download
.EXAMPLE
    .\scripts\install_service.ps1
#>

$ErrorActionPreference = "Stop"

# ─── Configuration ───────────────────────────────────────────
$ServiceName    = "ClaudeSecretary"
$DisplayName    = "Claude AI Secretary"
$Description    = "24/7 autonomous AI email secretary — Claude via copilot-api"
$ProjectDir     = $PSScriptRoot | Split-Path -Parent
$PythonExe      = "$ProjectDir\.venv\Scripts\pythonw.exe"
$LogDir         = "$ProjectDir\logs"
# ─────────────────────────────────────────────────────────────

# Verify NSSM is available
if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "NSSM not found. Install: choco install nssm"
    exit 1
}

# Ensure log directory exists
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Remove existing service if present
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[*] Stopping existing service..."
    nssm stop $ServiceName 2>$null
    Start-Sleep -Seconds 2
    Write-Host "[*] Removing existing service..."
    nssm remove $ServiceName confirm
    Start-Sleep -Seconds 1
}

# Install
Write-Host "[+] Installing service: $ServiceName"
nssm install $ServiceName $PythonExe
nssm set $ServiceName AppParameters "-m secretary watch"

# Working directory
nssm set $ServiceName AppDirectory $ProjectDir

# Display name & description
nssm set $ServiceName DisplayName $DisplayName
nssm set $ServiceName Description $Description

# ─── Restart policy ──────────────────────────────────────────
nssm set $ServiceName AppExit Default Restart
nssm set $ServiceName AppRestartDelay 5000

# Windows Service recovery: restart on 1st, 2nd, 3rd failure
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/10000/restart/30000

# ─── Shutdown behavior ───────────────────────────────────────
# Send Ctrl+C first, wait 15s for graceful shutdown, then kill
nssm set $ServiceName AppStopMethodSkip 0
nssm set $ServiceName AppStopMethodConsole 15000
nssm set $ServiceName AppStopMethodWindow 15000
nssm set $ServiceName AppStopMethodThreads 15000

# ─── Logging ─────────────────────────────────────────────────
nssm set $ServiceName AppStdout "$LogDir\secretary-stdout.log"
nssm set $ServiceName AppStderr "$LogDir\secretary-stderr.log"
nssm set $ServiceName AppStdoutCreationDisposition 4
nssm set $ServiceName AppStderrCreationDisposition 4
nssm set $ServiceName AppRotateFiles 1
nssm set $ServiceName AppRotateOnline 1
nssm set $ServiceName AppRotateBytes 10485760

# ─── Environment variables ───────────────────────────────────
nssm set $ServiceName AppEnvironmentExtra `
    "PYTHONUNBUFFERED=1" `
    "SECRETARY_CONFIG=$ProjectDir\config.yaml"

# ─── Startup type ────────────────────────────────────────────
nssm set $ServiceName Start SERVICE_AUTO_START

Write-Host ""
Write-Host "================================================================"
Write-Host "  Service '$ServiceName' installed."
Write-Host ""
Write-Host "  Start:   nssm start $ServiceName"
Write-Host "  Stop:    nssm stop $ServiceName"
Write-Host "  Status:  nssm status $ServiceName"
Write-Host "  Logs:    $LogDir\"
Write-Host "  Edit:    nssm edit $ServiceName  (opens GUI)"
Write-Host "  Remove:  nssm remove $ServiceName confirm"
Write-Host "================================================================"
