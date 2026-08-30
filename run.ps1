# run.ps1 - launch orgtree locally with the OpenRouter provider enabled.
#
#   .\run.ps1              build the frontend, start the server DETACHED
#                          (its own window), smoke-check, and return
#   .\run.ps1 -Foreground  run in THIS window; Ctrl+C stops it
#   .\run.ps1 -Dev         also start the vite dev server on :5173 (hot reload)
#   .\run.ps1 -NoBuild     skip the frontend build (use an existing dist/)
#   .\run.ps1 -Port 7370   use a different backend port
#   .\run.ps1 -Stop        stop a server this script started (and any vite)
#
# Detached is the default so the server outlives the terminal that launched it.
# Reads OPENROUTER_API_KEY (and other KEY = "value" lines) from .env so the
# OpenRouter provider comes up connected. Org data lands in $env:ORGTREE_DATA
# (default ~/orgtree); nothing here is committed state.

[CmdletBinding()]
param(
  [switch]$Foreground,
  [switch]$Dev,
  [switch]$NoBuild,
  [switch]$Stop,
  [int]$Port = 7360
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backendDir = Join-Path $root 'backend'   # the orgtree package lives here
$feDir = Join-Path $root 'frontend'
$pidFile = Join-Path $root '.run-pids'
Set-Location $root

function Stop-Run {
  $killed = 0
  if (Test-Path $pidFile) {
    foreach ($line in Get-Content $pidFile) {
      $procId = 0
      if ([int]::TryParse($line.Trim(), [ref]$procId) -and $procId -gt 0) {
        try {
          $p = Get-Process -Id $procId -ErrorAction Stop
          Stop-Process -Id $procId -Force
          Write-Host "stopped pid $procId ($($p.ProcessName))" -ForegroundColor Green
          $killed++
        } catch { }
      }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
  }
  # belt: anything still serving orgtree.api that we may have lost track of
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*orgtree.api*' } |
    ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force; Write-Host "stopped stray pid $($_.ProcessId)" -ForegroundColor Green; $killed++ } catch { }
    }
  if ($killed -eq 0) { Write-Host "nothing to stop" -ForegroundColor Yellow }
}

if ($Stop) { Stop-Run; return }

# --- refuse to double-launch ---
$base = "http://127.0.0.1:$Port"
try {
  Invoke-RestMethod "$base/api/host" -TimeoutSec 2 | Out-Null
  Write-Host "something is already serving $base - run '.\run.ps1 -Stop' first, or use -Port" -ForegroundColor Yellow
  return
} catch { }

# --- .env -> process env (format is: KEY = "value", CRLF, quoted) ---
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
  foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $k = $Matches[1]
      $v = $Matches[2].Trim().Trim('"').Trim("'")
      if ($v) { [Environment]::SetEnvironmentVariable($k, $v, 'Process') }
    }
  }
  if ($env:OPENROUTER_API_KEY) {
    $kp = $env:OPENROUTER_API_KEY.Substring(0, 7)
    Write-Host "OPENROUTER_API_KEY loaded ($kp..., $($env:OPENROUTER_API_KEY.Length) chars)" -ForegroundColor Green
  } else {
    Write-Host "no OPENROUTER_API_KEY in .env - OpenRouter will show as 'API key not set'" -ForegroundColor Yellow
  }
} else {
  Write-Host "no .env file - set OPENROUTER_API_KEY in the environment to enable OpenRouter" -ForegroundColor Yellow
}

$env:ORGTREE_PORT = "$Port"

# --- Claude CLI: avoid the .CMD shim (cmd.exe splits a path with a space) ---
# supervisor._claude_argv() falls back to `cmd /c claude.CMD` when no cli.js
# sits beside the resolved CLI; on a path like C:\Users\First Last that breaks.
# Point ORGTREE_CLAUDE at the native binary when we can find one.
if (-not $env:ORGTREE_CLAUDE) {
  $candidates = @()
  $onPath = (Get-Command claude -ErrorAction SilentlyContinue).Source
  if ($onPath) {
    $candidates += (Join-Path (Split-Path $onPath) 'node_modules\@anthropic-ai\claude-code\bin\claude.exe')
  }
  $candidates += (Join-Path $env:APPDATA 'npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe')
  $native = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if ($native) {
    $env:ORGTREE_CLAUDE = $native
    Write-Host "ORGTREE_CLAUDE -> $native" -ForegroundColor Green
  } elseif ($onPath -and $onPath.ToLower().EndsWith('.cmd')) {
    Write-Host "warning: Claude CLI resolves to a .CMD shim and no native claude.exe was found - Claude-tier turns may fail on a spaced path. Set ORGTREE_CLAUDE to a native binary. (OpenRouter tiers are unaffected.)" -ForegroundColor Yellow
  }
}

# --- backend deps ---
$py = 'python'
$haveDeps = $false
try {
  & $py -c "import fastapi, uvicorn, httpx" 2>$null
  $haveDeps = ($LASTEXITCODE -eq 0)
} catch { }
if (-not $haveDeps) {
  Write-Host "installing backend requirements..." -ForegroundColor Cyan
  & $py -m pip install -r (Join-Path $root 'requirements.txt')
}

# --- frontend ---
$distIx = Join-Path $feDir 'dist\index.html'
if (-not $Dev) {
  if ((-not $NoBuild) -or (-not (Test-Path $distIx))) {
    if (-not (Test-Path (Join-Path $feDir 'node_modules'))) {
      Write-Host "installing frontend deps..." -ForegroundColor Cyan
      Push-Location $feDir; npm install --no-audit --no-fund; Pop-Location
    }
    Write-Host "building the frontend..." -ForegroundColor Cyan
    Push-Location $feDir; npm run build; Pop-Location
  }
}

# --- launch ---
# $Foreground: child shares THIS console, dies with it (and we clean up).
# default:     child gets its OWN window and is NOT tied to this shell -
#              close that window, or run '.\run.ps1 -Stop', to end it.
$spawn = @{ FilePath = $py; ArgumentList = @('-m', 'orgtree.api'); WorkingDirectory = $backendDir; PassThru = $true }
if ($Foreground) { $spawn.NoNewWindow = $true }
Write-Host "starting the backend on $base ..." -ForegroundColor Cyan
$backend = Start-Process @spawn
Set-Content -Path $pidFile -Value $backend.Id

$vite = $null
if ($Dev) {
  $vspawn = @{ FilePath = 'npm'; ArgumentList = @('run', 'dev'); WorkingDirectory = $feDir; PassThru = $true }
  if ($Foreground) { $vspawn.NoNewWindow = $true }
  Write-Host "starting the vite dev server on http://127.0.0.1:5173 ..." -ForegroundColor Cyan
  $vite = Start-Process @vspawn
  Add-Content -Path $pidFile -Value $vite.Id
}

# --- wait for the API, then smoke-check the OpenRouter provider ---
$up = $false
foreach ($i in 1..60) {
  if ($backend.HasExited) { break }
  try { Invoke-RestMethod "$base/api/host" -TimeoutSec 2 | Out-Null; $up = $true; break }
  catch { Start-Sleep -Milliseconds 500 }
}
if (-not $up) {
  Write-Host "backend did not answer on $base within 30s" -ForegroundColor Red
  Stop-Run
  exit 1
}

try {
  $prov = (Invoke-RestMethod "$base/api/providers").providers | Where-Object { $_.id -eq 'openrouter' }
  $models = (Invoke-RestMethod "$base/api/providers/openrouter/models").models
  $bands = ($prov.tiers | ForEach-Object { "$($_.tier)/$($_.seat)" }) -join '  '
  $reason = if ($prov.reason) { $prov.reason } else { '(none - hireable)' }
  Write-Host ""
  Write-Host "OpenRouter provider:" -ForegroundColor Green
  Write-Host "  hire_enabled : $($prov.hire_enabled)"
  Write-Host "  reason       : $reason"
  Write-Host "  bands        : $bands"
  Write-Host "  catalogue    : $($models.Count) tool-capable models"
} catch {
  Write-Host "provider smoke check failed: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("=" * 60)
Write-Host "  open: $base" -ForegroundColor Green
if ($Dev) { Write-Host "  UI (hot reload): http://127.0.0.1:5173" }
if ($Foreground) {
  Write-Host "  Ctrl+C to stop"
  Write-Host ("=" * 60)
  try { Wait-Process -Id $backend.Id }
  finally { Stop-Run }
} else {
  Write-Host "  backend pid $($backend.Id) (its own window) - stop with:  .\run.ps1 -Stop" -ForegroundColor Green
  Write-Host ("=" * 60)
}
