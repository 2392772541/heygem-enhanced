$ErrorActionPreference = 'Continue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir

$logDir = Join-Path $projectRoot 'logs'
if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir 'start-heygem.log'

# Configuration resolution
$composeDir = if ($env:HEYGEM_COMPOSE_DIR -and (Test-Path $env:HEYGEM_COMPOSE_DIR)) {
  $env:HEYGEM_COMPOSE_DIR
} elseif (Test-Path (Join-Path $projectRoot 'deploy')) {
  Join-Path $projectRoot 'deploy'
} else {
  'D:\HeyGem-install\deploy'
}

$modeFile = if ($env:HEYGEM_MODE_FILE) {
  $env:HEYGEM_MODE_FILE
} else {
  Join-Path $projectRoot 'tts-mode.txt'
}

$adapterScript = Join-Path $projectRoot 'adapter\heygem_index_tts2_adapter.py'
$adapterPidFile = Join-Path $projectRoot 'heygem-index-tts2.pid'
$adapterStdoutLog = Join-Path $logDir 'heygem-index-tts2.out.log'
$adapterStderrLog = Join-Path $logDir 'heygem-index-tts2.err.log'

$queueScript = Join-Path $projectRoot 'core\heygem_queue_runner.py'
$queuePidFile = Join-Path $projectRoot 'heygem-queue-daemon.pid'
$queueStdoutLog = Join-Path $logDir 'heygem-queue-daemon.out.log'
$queueStderrLog = Join-Path $logDir 'heygem-queue-daemon.err.log'

$queueGuardScript = Join-Path $projectRoot 'core\heygem-queue-guard.ps1'
$queueGuardPidFile = Join-Path $projectRoot 'heygem-queue-guard.pid'
$queueGuardLog = Join-Path $logDir 'heygem-queue-guard.log'

$allContainers = @(
  'duix-avatar-tts',
  'duix-avatar-asr',
  'duix-avatar-gen-video'
)
$indexModeContainers = @(
  'duix-avatar-asr',
  'duix-avatar-gen-video'
)
$logTrimMaxBytes = 2097152
$logTrimKeepBytes = 1048576

function Trim-LogFile {
  param(
    [string]$Path,
    [int]$MaxBytes,
    [int]$KeepBytes
  )

  if (-not (Test-Path $Path)) {
    return
  }

  try {
    $item = Get-Item $Path -ErrorAction Stop
    if ($item.Length -le $MaxBytes) {
      return
    }

    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
      $length = [int64]$fs.Length
      $readBytes = [Math]::Min($KeepBytes, [int]$length)
      $fs.Seek(-$readBytes, [System.IO.SeekOrigin]::End) | Out-Null
      $buffer = New-Object byte[] $readBytes
      [void]$fs.Read($buffer, 0, $readBytes)
    } finally {
      $fs.Close()
    }

    $text = [System.Text.Encoding]::UTF8.GetString($buffer)
    $newlineIndex = $text.IndexOf([Environment]::NewLine)
    if ($newlineIndex -ge 0 -and $newlineIndex + 2 -lt $text.Length) {
      $text = $text.Substring($newlineIndex + 2)
    }
    Set-Content -Path $Path -Value $text -Encoding UTF8
  } catch {
  }
}

foreach ($path in @($queueGuardLog, $queueStdoutLog, $queueStderrLog, $adapterStdoutLog, $adapterStderrLog, $logPath)) {
  Trim-LogFile -Path $path -MaxBytes $logTrimMaxBytes -KeepBytes $logTrimKeepBytes
}
Set-Content -Path $logPath -Value '' -Encoding UTF8

function Write-Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Write-Host $line
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

function Get-TtsMode {
  if (-not (Test-Path $modeFile)) {
    return 'fish-speech'
  }

  $mode = (Get-Content $modeFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($mode) {
    $mode = $mode.Trim().ToLowerInvariant()
  }
  if ($mode -in @('index-tts2', 'fish-speech')) {
    return $mode
  }

  return 'fish-speech'
}

function Resolve-DockerDesktop {
  if ($env:DOCKER_DESKTOP_PATH -and (Test-Path $env:DOCKER_DESKTOP_PATH)) {
    return $env:DOCKER_DESKTOP_PATH
  }
  $standard = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
  if (Test-Path $standard) {
    return $standard
  }
  return $null
}

function Resolve-ClientExe {
  if ($env:HEYGEM_CLIENT_EXE -and (Test-Path $env:HEYGEM_CLIENT_EXE)) {
    return $env:HEYGEM_CLIENT_EXE
  }
  $candidates = @(
    'C:\Program Files\Duix.Avatar\Duix.Avatar.exe',
    (Join-Path $env:ProgramFiles 'Duix.Avatar\Duix.Avatar.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Duix.Avatar\Duix.Avatar.exe')
  )
  foreach ($cand in $candidates) {
    if ($cand -and (Test-Path $cand)) {
      return $cand
    }
  }
  return $candidates[0]
}

function Resolve-DuixDataDir {
  if ($env:DUIX_DATA_DIR -and (Test-Path $env:DUIX_DATA_DIR)) {
    return $env:DUIX_DATA_DIR
  }
  foreach ($cand in @('D:\duix_avatar_data', 'C:\duix_avatar_data', 'E:\duix_avatar_data')) {
    if (Test-Path $cand) {
      return $cand
    }
  }
  if (Test-Path 'D:\') {
    return 'D:\duix_avatar_data'
  }
  return 'C:\duix_avatar_data'
}


function Get-QueuePython {
  $override = [string]$env:HEYGEM_QUEUE_PYTHON
  if ($override) { $override = $override.Trim() }
  if ($override -and (Test-Path $override)) { return $override }

  $pyCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pyCmd -and (Test-Path $pyCmd.Source)) { return $pyCmd.Source }

  $uvPythonPattern = Join-Path $env:APPDATA 'uv\python\cpython-*\python.exe'
  $uvFound = Resolve-Path $uvPythonPattern -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($uvFound) { return $uvFound.Path }

  $adapterVenv = Join-Path $projectRoot 'adapter\venv\Scripts\python.exe'
  if (Test-Path $adapterVenv) { return $adapterVenv }

  $fallbackAdapter = 'D:\HeyGem\tts-story-f5\venv\Scripts\python.exe'
  if (Test-Path $fallbackAdapter) { return $fallbackAdapter }

  return 'python.exe'
}

function Get-AdapterPython {
  $override = [string]$env:HEYGEM_ADAPTER_PYTHON
  if ($override) { $override = $override.Trim() }
  if ($override -and (Test-Path $override)) { return $override }

  $adapterVenv = Join-Path $projectRoot 'adapter\venv\Scripts\python.exe'
  if (Test-Path $adapterVenv) { return $adapterVenv }

  $fallbackAdapter = 'D:\HeyGem\tts-story-f5\venv\Scripts\python.exe'
  if (Test-Path $fallbackAdapter) { return $fallbackAdapter }

  return (Get-QueuePython)
}

function Test-DockerReady {
  cmd.exe /c "docker version --format ""{{.Server.Version}}"" >nul 2>nul" | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Wait-DockerReady {
  param([int]$TimeoutSeconds = 180)

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-DockerReady) {
      return $true
    }
    Start-Sleep -Seconds 2
  }

  return $false
}

function Wait-ContainersReady {
  param(
    [string[]]$Names,
    [int]$TimeoutSeconds = 120
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    $allReady = $true
    foreach ($name in $Names) {
      $running = docker inspect -f '{{.State.Running}}' $name 2>$null
      if ($LASTEXITCODE -ne 0 -or $running.Trim() -ne 'true') {
        $allReady = $false
        break
      }
    }

    if ($allReady) {
      return $true
    }

    Start-Sleep -Seconds 2
  }

  return $false
}

function Wait-HttpReady {
  param([int]$TimeoutSeconds = 180)

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri 'http://127.0.0.1:18180/healthz' -UseBasicParsing -TimeoutSec 5
      if ($response.StatusCode -eq 200) {
        return $true
      }
    } catch {
    }

    Start-Sleep -Seconds 2
  }

  return $false
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
    $_.CommandLine -like "*$adapterScript*"
  }
  foreach ($process in $processes) {
    $pids += [int]$process.ProcessId
  }

  $pids = $pids | Sort-Object -Unique
  foreach ($pidValue in $pids) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

function Start-IndexTtsAdapter {
  if (-not (Test-Path $adapterScript)) {
    throw "IndexTTS2 adapter script not found: $adapterScript"
  }

  $adapterPython = Get-AdapterPython
  if (-not (Test-Path $adapterPython) -and $adapterPython -ne 'python.exe') {
    throw "Python for IndexTTS2 adapter not found: $adapterPython"
  }

  Stop-IndexTtsAdapter

  Set-Content -Path $adapterStdoutLog -Value '' -Encoding UTF8
  Set-Content -Path $adapterStderrLog -Value '' -Encoding UTF8

  $adapterWorkDir = Split-Path -Parent $adapterScript
  $process = Start-Process `
    -FilePath $adapterPython `
    -ArgumentList @($adapterScript) `
    -WorkingDirectory $adapterWorkDir `
    -RedirectStandardOutput $adapterStdoutLog `
    -RedirectStandardError $adapterStderrLog `
    -PassThru

  Set-Content -Path $adapterPidFile -Value $process.Id -Encoding ASCII

  if (-not (Wait-HttpReady -TimeoutSeconds 180)) {
    throw 'IndexTTS2 compatibility service failed to become ready on http://127.0.0.1:18180/healthz within 180 seconds.'
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
    $_.CommandLine -like "*$queueScript*"
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
    $_.CommandLine -like "*$queueGuardScript*"
  }
  foreach ($process in $processes) {
    $pids += [int]$process.ProcessId
  }

  $pids = $pids | Sort-Object -Unique
  foreach ($pidValue in $pids) {
    Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
  }
}

function Initialize-QueueDatabase {
  if (-not (Test-Path $queueScript)) {
    throw "Queue daemon script not found: $queueScript"
  }

  $queuePython = Get-QueuePython
  $env:HEYGEM_MODE_FILE = $modeFile

  for ($attempt = 1; $attempt -le 3; $attempt++) {
    Write-Log ("Initializing queue SQLite triggers (attempt {0}/3)..." -f $attempt)
    & $queuePython $queueScript --init-triggers
    if ($LASTEXITCODE -eq 0) {
      Write-Log 'Queue SQLite triggers are ready.'
      return
    }
    Start-Sleep -Seconds 2
  }

  throw 'Queue SQLite trigger initialization failed.'
}

function Start-QueueGuard {
  if (-not (Test-Path $queueGuardScript)) {
    throw "Queue guard script not found: $queueGuardScript"
  }

  Stop-QueueGuard

  if (-not (Test-Path $queueGuardLog)) {
    Set-Content -Path $queueGuardLog -Value '' -Encoding UTF8
  }

  $env:HEYGEM_MODE_FILE = $modeFile
  $process = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $queueGuardScript) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

  Set-Content -Path $queueGuardPidFile -Value $process.Id -Encoding ASCII
}

# --- Main execution flow ---
$dockerDesktop = Resolve-DockerDesktop
$clientExe = Resolve-ClientExe

if (-not (Test-Path $composeDir)) {
  throw "Compose directory not found: $composeDir"
}

$selectedMode = Get-TtsMode
Write-Log ("Selected TTS mode: {0}" -f $selectedMode)
Write-Log ("Queue mode file: {0}" -f $modeFile)
Write-Log ("Queue Python: {0}" -f (Get-QueuePython))

Write-Log 'Checking Docker readiness...'
if (-not (Test-DockerReady)) {
  if ($dockerDesktop -and (Test-Path $dockerDesktop)) {
    Write-Log 'Docker is not ready. Starting Docker Desktop...'
    Start-Process -FilePath $dockerDesktop | Out-Null
    if (-not (Wait-DockerReady)) {
      throw 'Docker Desktop did not become ready within 180 seconds.'
    }
  } else {
    throw 'Docker daemon is not running and Docker Desktop executable could not be found.'
  }
}

Write-Log 'Docker is ready.'
$resolvedDataDir = Resolve-DuixDataDir
$env:DUIX_DATA_DIR = $resolvedDataDir.Replace('\', '/')
Write-Log ("Resolved Duix data dir: {0}" -f $resolvedDataDir)
Write-Log 'Starting HeyGem container services...'
Push-Location $composeDir
try {
  if ($selectedMode -eq 'index-tts2') {
    & docker compose up -d duix-avatar-asr duix-avatar-gen-video 2>&1 | ForEach-Object {
      Write-Log $_
    }
  } else {
    & docker compose up -d 2>&1 | ForEach-Object {
      Write-Log $_
    }
  }
  if ($LASTEXITCODE -ne 0) {
    throw 'docker compose up failed.'
  }

  if ($selectedMode -eq 'index-tts2') {
    $stopTtsOutput = docker compose stop duix-avatar-tts 2>&1
    $stopTtsOutput | ForEach-Object {
      Write-Log $_
    }
  }
} finally {
  Pop-Location
}

if ($selectedMode -eq 'index-tts2') {
  if (-not (Wait-ContainersReady -Names $indexModeContainers)) {
    throw 'HeyGem video containers did not reach running state in time.'
  }

  Write-Log 'Starting local IndexTTS2 compatibility service...'
  Start-IndexTtsAdapter
  Write-Log 'IndexTTS2 compatibility service is ready.'
} else {
  Stop-IndexTtsAdapter
  if (-not (Wait-ContainersReady -Names $allContainers)) {
    throw 'HeyGem containers did not all reach running state in time.'
  }
}

Initialize-QueueDatabase

Write-Log 'Stopping any previous queue workers...'
Stop-QueueGuard
Stop-QueueDaemon

Write-Log 'Starting queue guard daemon...'
Start-QueueGuard
Write-Log 'Queue guard is running.'

if (Test-Path $clientExe) {
  Write-Log 'Launching HeyGem client...'
  Start-Process -FilePath $clientExe | Out-Null
} else {
  Write-Log "HeyGem client executable not found at $clientExe (You can start it manually when installed)."
}

Write-Log 'Done. HeyGem-Enhanced environment is fully initialized.'
