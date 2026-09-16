$ErrorActionPreference = 'Continue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$guardScriptPath = $MyInvocation.MyCommand.Path

$logDir = Join-Path $projectRoot 'logs'
if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$queueScript = Join-Path $scriptDir 'heygem_queue_runner.py'
$modeFile = if ($env:HEYGEM_MODE_FILE) {
  $env:HEYGEM_MODE_FILE
} elseif (Test-Path (Join-Path $projectRoot 'tts-mode.txt')) {
  Join-Path $projectRoot 'tts-mode.txt'
} else {
  Join-Path $projectRoot 'tts-mode.txt'
}

$queuePidFile = Join-Path $projectRoot 'heygem-queue-daemon.pid'
$queueStdoutLog = Join-Path $logDir 'heygem-queue-daemon.out.log'
$queueStderrLog = Join-Path $logDir 'heygem-queue-daemon.err.log'
$guardLog = Join-Path $logDir 'heygem-queue-guard.log'

$appDataDir = if ($env:APPDATA) { $env:APPDATA } else { Join-Path [Environment]::GetFolderPath('ApplicationData') 'Duix.Avatar' }
$appLogPath = if ($env:HEYGEM_MAIN_LOG) { $env:HEYGEM_MAIN_LOG } else { Join-Path $appDataDir 'Duix.Avatar\logs\main.log' }

$recent500WindowSeconds = 180
$recent500Threshold = 3
$ttsRestartCooldownSeconds = 180
$script:lastTtsRestartAt = [datetime]'2000-01-01 00:00:00'
$pollSeconds = 10
$logTrimMaxBytes = 2097152
$logTrimKeepBytes = 1048576

if (-not (Test-Path $guardLog)) {
  Set-Content -Path $guardLog -Value '' -Encoding UTF8
}

function Write-GuardLog {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Add-Content -Path $guardLog -Value $line -Encoding UTF8
  Trim-LogFile -Path $guardLog -MaxBytes $logTrimMaxBytes -KeepBytes $logTrimKeepBytes
}

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

function Get-GuardProcesses {
  return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*$guardScriptPath*"
  }
}

function Get-QueueDaemonProcesses {
  return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*$queueScript*" -and $_.CommandLine -like '*--daemon*'
  }
}

function Ensure-SingleGuardInstance {
  $allGuards = @(Get-GuardProcesses)
  if ($allGuards.Count -le 1) {
    return $true
  }

  $ordered = $allGuards | Sort-Object ProcessId
  $keep = $ordered | Select-Object -First 1
  if ([int]$keep.ProcessId -ne [int]$PID) {
    return $false
  }

  $duplicates = $ordered | Select-Object -Skip 1
  foreach ($dup in $duplicates) {
    Stop-Process -Id $dup.ProcessId -Force -ErrorAction SilentlyContinue
    Write-GuardLog "stopped duplicate guard pid=$($dup.ProcessId)"
  }

  return $true
}

function Get-QueuePython {
  $override = [string]$env:HEYGEM_QUEUE_PYTHON
  if ($override) {
    $override = $override.Trim()
  }
  if ($override -and (Test-Path $override)) {
    return $override
  }

  $pyCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($pyCmd -and (Test-Path $pyCmd.Source)) {
    return $pyCmd.Source
  }

  $uvPythonPattern = Join-Path $env:APPDATA 'uv\python\cpython-*\python.exe'
  $uvFound = Resolve-Path $uvPythonPattern -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($uvFound) {
    return $uvFound.Path
  }

  $adapterVenv = Join-Path $projectRoot 'adapter\venv\Scripts\python.exe'
  if (Test-Path $adapterVenv) {
    return $adapterVenv
  }

  $fallbackAdapter = 'D:\HeyGem\tts-story-f5\venv\Scripts\python.exe'
  if (Test-Path $fallbackAdapter) {
    return $fallbackAdapter
  }

  return 'python.exe'
}

function Start-QueueDaemonInstance {
  if (-not (Test-Path $queueScript)) {
    Write-GuardLog "queue start skipped: script not found ($queueScript)"
    return
  }

  $queuePython = Get-QueuePython
  if (-not $queuePython) {
    Write-GuardLog "queue start skipped: python not found"
    return
  }

  try {
    $env:HEYGEM_MODE_FILE = $modeFile
    Set-Content -Path $queueStdoutLog -Value '' -Encoding UTF8
    Set-Content -Path $queueStderrLog -Value '' -Encoding UTF8
    $process = Start-Process `
      -FilePath $queuePython `
      -ArgumentList @($queueScript, '--daemon') `
      -WorkingDirectory (Split-Path -Parent $queueScript) `
      -RedirectStandardOutput $queueStdoutLog `
      -RedirectStandardError $queueStderrLog `
      -PassThru
    Set-Content -Path $queuePidFile -Value $process.Id -Encoding ASCII
    Write-GuardLog "queue daemon started pid=$($process.Id) python=$queuePython"
  } catch {
    Write-GuardLog "queue daemon start failed: $($_.Exception.Message)"
  }
}

function Ensure-QueueDaemon {
  $allProcesses = @(Get-QueueDaemonProcesses)
  if ($allProcesses.Count -eq 0) {
    Start-QueueDaemonInstance
    return
  }

  $allIds = @{}
  foreach ($proc in $allProcesses) {
    $allIds[[int]$proc.ProcessId] = $true
  }

  $rootProcesses = @($allProcesses | Where-Object { -not $allIds.ContainsKey([int]$_.ParentProcessId) })
  if ($rootProcesses.Count -eq 0) {
    $rootProcesses = @($allProcesses | Sort-Object ProcessId | Select-Object -First 1)
  }

  $orderedRoots = $rootProcesses | Sort-Object ProcessId
  $keep = $orderedRoots | Select-Object -First 1
  $duplicates = $orderedRoots | Select-Object -Skip 1
  foreach ($dup in $duplicates) {
    Stop-Process -Id $dup.ProcessId -Force -ErrorAction SilentlyContinue
    Write-GuardLog "stopped duplicate root queue daemon pid=$($dup.ProcessId)"
  }
  Set-Content -Path $queuePidFile -Value $keep.ProcessId -Encoding ASCII
}

function Get-RecentTts500Count {
  if (-not (Test-Path $appLogPath)) {
    return 0
  }

  $cutoff = (Get-Date).AddSeconds(-$recent500WindowSeconds)
  $count = 0
  $lines = Get-Content -LiteralPath $appLogPath -Tail 400 -ErrorAction SilentlyContinue
  foreach ($line in $lines) {
    if ($line -notlike '*synthesisVideo ~ error: Request failed with status code 500*') {
      continue
    }
    if ($line -match '^(?<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
      try {
        $ts = [datetime]::ParseExact($Matches.ts, 'yyyy-MM-dd HH:mm:ss', $null)
        if ($ts -ge $cutoff) {
          $count++
        }
      } catch {
      }
    }
  }
  return $count
}

function Maybe-RestartTtsOnBurst500 {
  $recent500 = Get-RecentTts500Count
  if ($recent500 -lt $recent500Threshold) {
    return
  }

  $elapsed = ((Get-Date) - $script:lastTtsRestartAt).TotalSeconds
  if ($elapsed -lt $ttsRestartCooldownSeconds) {
    return
  }

  try {
    $restartOutput = docker restart duix-avatar-tts 2>&1
    if ($LASTEXITCODE -eq 0) {
      Write-GuardLog "restarted duix-avatar-tts due to repeated 500 errors ($recent500 in ${recent500WindowSeconds}s)"
      $script:lastTtsRestartAt = Get-Date
    } else {
      Write-GuardLog "failed to restart duix-avatar-tts on repeated 500 errors: $restartOutput"
    }
  } catch {
    Write-GuardLog "failed to restart duix-avatar-tts on repeated 500 errors: $($_.Exception.Message)"
  }
}

Write-GuardLog 'queue guard started'
if (-not (Ensure-SingleGuardInstance)) {
  exit 0
}

while ($true) {
  try {
    Ensure-QueueDaemon
    Maybe-RestartTtsOnBurst500
  } catch {
    Write-GuardLog "guard loop error: $($_.Exception.Message)"
  }

  Start-Sleep -Seconds $pollSeconds
}
