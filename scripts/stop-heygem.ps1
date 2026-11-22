$ErrorActionPreference = 'Continue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir

$logDir = Join-Path $projectRoot 'logs'
if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir 'stop-heygem.log'
Set-Content -Path $logPath -Value '' -Encoding UTF8

$composeDir = if ($env:HEYGEM_COMPOSE_DIR -and (Test-Path $env:HEYGEM_COMPOSE_DIR)) {
  $env:HEYGEM_COMPOSE_DIR
} elseif (Test-Path (Join-Path $projectRoot 'deploy')) {
  Join-Path $projectRoot 'deploy'
} else {
  'D:\HeyGem-install\deploy'
}

$adapterScript = Join-Path $projectRoot 'adapter\heygem_index_tts2_adapter.py'
$adapterPidFile = Join-Path $projectRoot 'heygem-index-tts2.pid'
$queueScript = Join-Path $projectRoot 'core\heygem_queue_runner.py'
$queuePidFile = Join-Path $projectRoot 'heygem-queue-daemon.pid'
$queueGuardScript = Join-Path $projectRoot 'core\heygem-queue-guard.ps1'
$queueGuardPidFile = Join-Path $projectRoot 'heygem-queue-guard.pid'
$studioPidFile = Join-Path $projectRoot 'heygem-studio.pid'

function Write-Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Write-Host $line
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

function Stop-IndexTtsAdapter {
  $pids = @()

  if (Test-Path $adapterPidFile) {
    $pidValue = (Get-Content $adapterPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($pidValue -match '^\d+$') {
      $pids += [int]$pidValue
    }
    Remove-Item $adapterPidFile -Force -ErrorAction SilentlyContinue
  }

  $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*heygem_index_tts2_adapter.py*" -or $_.CommandLine -like "*indextts2_studio_worker.py*"
  }
  foreach ($process in $processes) {
    $pids += [int]$process.ProcessId
  }

  $pids = $pids | Sort-Object -Unique
  foreach ($pidValue in $pids) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

function Stop-QueueDaemon {
  $pids = @()

  if (Test-Path $queuePidFile) {
    $pidValue = (Get-Content $queuePidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($pidValue -match '^\d+$') {
      $pids += [int]$pidValue
    }
    Remove-Item $queuePidFile -Force -ErrorAction SilentlyContinue
  }

  $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*heygem_queue_runner.py*"
  }
  foreach ($process in $processes) {
    $pids += [int]$process.ProcessId
  }

  $pids = $pids | Sort-Object -Unique
  foreach ($pidValue in $pids) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

function Stop-QueueGuard {
  $pids = @()

  if (Test-Path $queueGuardPidFile) {
    $pidValue = (Get-Content $queueGuardPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($pidValue -match '^\d+$') {
      $pids += [int]$pidValue
    }
    Remove-Item $queueGuardPidFile -Force -ErrorAction SilentlyContinue
  }

  $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*heygem-queue-guard.ps1*"
  }
  foreach ($process in $processes) {
    $pids += [int]$process.ProcessId
  }

  $pids = $pids | Sort-Object -Unique
  foreach ($pidValue in $pids) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

function Stop-StudioServer {
  $pids = @()
  if (Test-Path $studioPidFile) {
    $pidValue = (Get-Content $studioPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($pidValue -match '^\d+$') { $pids += [int]$pidValue }
    Remove-Item $studioPidFile -Force -ErrorAction SilentlyContinue
  }
  $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like '*studio.server*'
  }
  foreach ($process in $processes) { $pids += [int]$process.ProcessId }
  foreach ($pidValue in ($pids | Sort-Object -Unique)) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

cmd.exe /c "docker version --format ""{{.Server.Version}}"" >nul 2>nul" | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Log 'Docker is not running. Cleaning up host processes...'
  Stop-QueueGuard
  Stop-QueueDaemon
  Stop-IndexTtsAdapter
  Stop-StudioServer
  Get-Process -Name 'Duix.Avatar' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  Write-Log 'Done.'
  exit 0
}

Write-Log 'Stopping HeyGem client if it is running...'
Get-Process -Name 'Duix.Avatar' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Log 'Stopping queue guard daemon...'
Stop-QueueGuard

Write-Log 'Stopping queue runner daemon...'
Stop-QueueDaemon

Write-Log 'Stopping local IndexTTS2 compatibility service if it is running...'
Stop-IndexTtsAdapter

Write-Log 'Stopping HeyGem Studio Web service...'
Stop-StudioServer

if (Test-Path $composeDir) {
  if (-not $env:DUIX_DATA_DIR) {
    foreach ($cand in @('D:\duix_avatar_data', 'C:\duix_avatar_data', 'E:\duix_avatar_data')) {
      if (Test-Path $cand) { $env:DUIX_DATA_DIR = $cand.Replace('\', '/'); break }
    }
  }
  Write-Log "Stopping Docker container services in $composeDir..."
  Push-Location $composeDir
  try {
    $stopOutput = docker compose stop 2>&1
    $stopOutput | ForEach-Object {
      Write-Log $_
    }
  } finally {
    Pop-Location
  }
} else {
  Write-Log "Compose directory not found: $composeDir"
}

Write-Log 'HeyGem services stopped successfully.'
