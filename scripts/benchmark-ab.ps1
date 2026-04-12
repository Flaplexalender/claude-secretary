# Multi-Instance A/B Benchmark
# Launches two Secretary instances: baseline (optimizations OFF) vs optimized (ON).
# Compare results with: secretary metrics show / secretary metrics benchmarks
#
# Prerequisites:
#   - copilot-api proxy running on localhost:4141
#   - Google OAuth configured (secretary auth)
#   - config.yaml present with base settings
#
# Usage:
#   .\scripts\benchmark-ab.ps1
#   .\scripts\benchmark-ab.ps1 -Campaign campaigns\benchmark-optimizations.yaml
#
# After both complete, compare:
#   .venv\Scripts\python.exe -m secretary metrics instances
#   .venv\Scripts\python.exe -m secretary metrics show
#   .venv\Scripts\python.exe -m secretary metrics benchmarks

param(
    [string]$Campaign = "campaigns\benchmark-optimizations.yaml",
    [int]$MaxRuns = 1
)

$python = ".venv\Scripts\python.exe"

Write-Host "`n=== Secretary A/B Benchmark ===" -ForegroundColor Cyan
Write-Host "Campaign: $Campaign"
Write-Host "Max runs per instance: $MaxRuns`n"

# Create baseline config (all optimizations OFF)
$baselineConfig = @"
anthropic_base_url: `${ANTHROPIC_BASE_URL:-http://localhost:4141}
agent_prefix: true
reasoning_effort: high
data_root: data
file_tools: true
routing:
  default_tier: high
  tiers:
    high:
      model: claude-opus-4.6
      max_turns: 30
      max_budget_usd: 5.0
watcher:
  interval_minutes: 1
  max_retries: 0
  task_timeout: 600
  campaign_file: $Campaign
optimizations:
  selective_tools: false
  turn_budget_signal: false
  context_preload: false
  conversation_summary: false
  dynamic_max_tokens: false
multi:
  coordinate: true
  shared_dir: data/shared
"@

# Create optimized config (all optimizations ON — default)
$optimizedConfig = @"
anthropic_base_url: `${ANTHROPIC_BASE_URL:-http://localhost:4141}
agent_prefix: true
reasoning_effort: high
data_root: data
file_tools: true
routing:
  default_tier: high
  tiers:
    high:
      model: claude-opus-4.6
      max_turns: 30
      max_budget_usd: 5.0
watcher:
  interval_minutes: 1
  max_retries: 0
  task_timeout: 600
  campaign_file: $Campaign
optimizations:
  selective_tools: true
  turn_budget_signal: true
  context_preload: true
  conversation_summary: true
  summary_after_turn: 5
  dynamic_max_tokens: true
multi:
  coordinate: true
  shared_dir: data/shared
"@

# Write temp configs
$baselineConfig | Out-File -Encoding utf8 "config-baseline.yaml"
$optimizedConfig | Out-File -Encoding utf8 "config-optimized.yaml"

Write-Host "[1/2] Launching BASELINE instance (all optimizations OFF)..." -ForegroundColor Yellow
$baselineJob = Start-Job -ScriptBlock {
    param($py, $cfg, $campaign, $maxRuns)
    Set-Location $using:PWD
    & $py -m secretary watch `
        --instance baseline `
        --coordinate `
        --campaign $campaign `
        --max-runs $maxRuns `
        --config $cfg 2>&1
} -ArgumentList $python, "config-baseline.yaml", $Campaign, $MaxRuns

Write-Host "[2/2] Launching OPTIMIZED instance (all optimizations ON)..." -ForegroundColor Green
$optimizedJob = Start-Job -ScriptBlock {
    param($py, $cfg, $campaign, $maxRuns)
    Set-Location $using:PWD
    & $py -m secretary watch `
        --instance optimized `
        --coordinate `
        --campaign $campaign `
        --max-runs $maxRuns `
        --config $cfg 2>&1
} -ArgumentList $python, "config-optimized.yaml", $Campaign, $MaxRuns

Write-Host "`nWaiting for both instances to complete..." -ForegroundColor Cyan

# Wait and show progress
$baselineJob, $optimizedJob | Wait-Job -Timeout 1800

Write-Host "`n=== BASELINE output ===" -ForegroundColor Yellow
Receive-Job $baselineJob

Write-Host "`n=== OPTIMIZED output ===" -ForegroundColor Green
Receive-Job $optimizedJob

# Cleanup
Remove-Job $baselineJob, $optimizedJob -Force -ErrorAction SilentlyContinue
Remove-Item "config-baseline.yaml", "config-optimized.yaml" -ErrorAction SilentlyContinue

Write-Host "`n=== Results ===" -ForegroundColor Cyan
Write-Host "Run these to see comparison:"
Write-Host "  & $python -m secretary metrics instances"
Write-Host "  & $python -m secretary metrics show"
Write-Host "  & $python -m secretary metrics benchmarks"
