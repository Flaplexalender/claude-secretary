# Auto-start Secretary Watcher on Windows boot
# Run this script once (elevated) to create the scheduled task.
# The watcher will start automatically when the user logs in.

$SecretaryRoot = "$env:USERPROFILE\claude-secretary"
$VenvPython = "$SecretaryRoot\.venv\Scripts\python.exe"
$TaskName = "ClaudeSecretaryWatcher"

# Check prerequisites
if (-not (Test-Path $VenvPython)) {
    Write-Host "ERROR: Python venv not found at $VenvPython" -ForegroundColor Red
    exit 1
}

# Build the command — start the proxy first, then the watcher
$WatcherScript = @"
`$env:Path = "C:\Program Files\GitHub CLI;" + `$env:Path
Set-Location "$SecretaryRoot"

# Start copilot-api proxy in background
`$proxy = Start-Process -NoNewWindow -PassThru -FilePath "npx" -ArgumentList "copilot-api@latest","start" -RedirectStandardOutput "data\proxy.log" -RedirectStandardError "data\proxy-error.log"
Start-Sleep -Seconds 10  # Wait for proxy to initialize

# Start watcher
& "$VenvPython" -m secretary watch --campaign "campaign.yaml,campaigns/self-build.yaml" 2>&1 | Tee-Object -FilePath "data\watcher.log" -Append
"@

$ScriptPath = "$SecretaryRoot\scripts\start-watcher.ps1"
$ScriptPath | Split-Path -Parent | ForEach-Object { New-Item $_ -ItemType Directory -Force -ErrorAction SilentlyContinue | Out-Null }
Set-Content -Path $ScriptPath -Value $WatcherScript -Encoding UTF8

# Register scheduled task
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`"" `
    -WorkingDirectory $SecretaryRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Check if task already exists
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Updating existing task '$TaskName'..."
    Set-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings
} else {
    Write-Host "Creating task '$TaskName'..."
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Auto-start Claude Secretary watcher daemon on login"
}

Write-Host "`nScheduled task '$TaskName' configured." -ForegroundColor Green
Write-Host "The watcher will start automatically on next login."
Write-Host "To start now: schtasks /run /tn '$TaskName'"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'"
