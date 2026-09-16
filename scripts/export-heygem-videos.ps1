﻿$ErrorActionPreference = 'Continue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir

$logDir = Join-Path $projectRoot 'logs'
if (-not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logPath = Join-Path $logDir 'export-heygem-videos.log'
Set-Content -Path $logPath -Value '' -Encoding UTF8

$dbPath = if ($env:HEYGEM_DB_OVERRIDE) {
  $env:HEYGEM_DB_OVERRIDE
} else {
  Join-Path $env:APPDATA 'Duix.Avatar\biz.db'
}

$renderRoot = if ($env:HEYGEM_OUTPUT_ROOT) {
  $env:HEYGEM_OUTPUT_ROOT
} elseif ($env:HEYGEM_OUT_OVERRIDE) {
  $env:HEYGEM_OUT_OVERRIDE
} elseif (Test-Path 'D:\duix_avatar_data\face2face\temp') {
  'D:\duix_avatar_data\face2face\temp'
} else {
  'C:\duix_avatar_data\face2face\temp'
}

$exportRoot = if ($env:HEYGEM_DELIVERY_ROOT) {
  $env:HEYGEM_DELIVERY_ROOT
} elseif (Test-Path 'E:\数字人视频文件') {
  'E:\数字人视频文件'
} elseif (Test-Path 'D:\数字人视频文件') {
  'D:\数字人视频文件'
} else {
  Join-Path [Environment]::GetFolderPath('Desktop') '数字人视频文件'
}
$exportDir = $exportRoot

function Write-Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  Write-Host $line
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

function Query-VideoRecords {
  param([string]$DbPath, [string]$Sql)

  $cmd = Get-Command sqlite3 -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -ne $cmd) {
    Write-Log "Using sqlite3 CLI: $($cmd.Source)"
    $rawJson = & $cmd.Source -json $DbPath $Sql
    if ($LASTEXITCODE -eq 0 -and (-not [string]::IsNullOrWhiteSpace($rawJson))) {
      return ($rawJson | ConvertFrom-Json)
    }
  }

  $pyCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -ne $pyCmd) {
    Write-Log "Using Python sqlite3 fallback: $($pyCmd.Source)"
    $pyScript = @"
import sqlite3, json, sys
conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute(sys.argv[2])
rows = [dict(r) for r in cursor.fetchall()]
print(json.dumps(rows, ensure_ascii=False))
"@
    $rawJson = & $pyCmd.Source -c $pyScript $DbPath $Sql
    if ($LASTEXITCODE -eq 0 -and (-not [string]::IsNullOrWhiteSpace($rawJson))) {
      return ($rawJson | ConvertFrom-Json)
    }
  }

  throw "Neither sqlite3 CLI nor python was found to query database $DbPath"
}

function Normalize-VideoRows {
  param([object]$Rows)
  if ($null -eq $Rows) {
    return @()
  }
  if ($Rows -is [System.Array]) {
    return $Rows
  }
  return @($Rows)
}

function Sanitize-FileName {
  param([string]$Name)

  $value = [string]$Name
  if ($null -eq $Name) {
    $value = ''
  }
  $value = $value.Trim()
  if ([string]::IsNullOrWhiteSpace($value)) {
    $value = 'video'
  }

  foreach ($ch in [System.IO.Path]::GetInvalidFileNameChars()) {
    $value = $value.Replace([string]$ch, '_')
  }

  $value = $value.Trim(' ', '.')
  if ([string]::IsNullOrWhiteSpace($value)) {
    $value = 'video'
  }

  if ($value.Length -gt 80) {
    $value = $value.Substring(0, 80).Trim()
  }

  return $value
}

function Resolve-SourceFile {
  param([object]$Row)

  $candidates = @()
  $filePath = [string]$Row.file_path
  $taskCode = [string]$Row.code
  if ($null -eq $Row.file_path) {
    $filePath = ''
  }
  if ($null -eq $Row.code) {
    $taskCode = ''
  }

  if (-not [string]::IsNullOrWhiteSpace($filePath)) {
    $normalized = $filePath.Replace('/', '\')
    if ([System.IO.Path]::IsPathRooted($normalized)) {
      $candidates += $normalized
    } else {
      $candidates += Join-Path $renderRoot ($normalized.TrimStart('\'))
    }
  }

  if (-not [string]::IsNullOrWhiteSpace($taskCode)) {
    $candidates += Join-Path $renderRoot ($taskCode + '-r.mp4')
  }

  foreach ($candidate in $candidates | Select-Object -Unique) {
    if (Test-Path -LiteralPath $candidate) {
      return $candidate
    }
  }

  return $null
}

function Build-TargetFilePath {
  param(
    [string]$Folder,
    [int]$Id,
    [string]$Name,
    [string]$Code
  )

  $safeName = Sanitize-FileName $Name
  $prefix = '{0:D4}' -f $Id

  $baseName = $prefix + '_' + $safeName
  if (-not [string]::IsNullOrWhiteSpace($Code)) {
    $shortCodeLength = [Math]::Min(8, $Code.Length)
    $baseName = $baseName + '_' + $Code.Substring(0, $shortCodeLength)
  }

  $target = Join-Path $Folder ($baseName + '.mp4')
  $index = 1
  while (Test-Path -LiteralPath $target) {
    $target = Join-Path $Folder ($baseName + '_' + $index + '.mp4')
    $index += 1
  }

  return $target
}

if (-not (Test-Path -LiteralPath $dbPath)) {
  throw "HeyGem DB not found: $dbPath"
}

if (-not (Test-Path -LiteralPath $renderRoot)) {
  throw "Render output folder not found: $renderRoot"
}

$sql = "SELECT id, ifnull(name, '') AS name, ifnull(code, '') AS code, ifnull(file_path, '') AS file_path FROM video WHERE status='success' ORDER BY id ASC;"
$parsed = Query-VideoRecords -DbPath $dbPath -Sql $sql
$rows = Normalize-VideoRows $parsed

if ($rows.Count -eq 0) {
  Write-Log 'No success videos found.'
  exit 0
}

New-Item -ItemType Directory -Path $exportDir -Force | Out-Null
Write-Log "Export folder: $exportDir"

$copied = 0
$missing = 0

foreach ($row in $rows) {
  $id = [int]$row.id
  $name = [string]$row.name
  $code = [string]$row.code

  $source = Resolve-SourceFile $row
  if ([string]::IsNullOrWhiteSpace($source)) {
    $missing += 1
    Write-Log "Skip id=$id (source file missing)"
    continue
  }

  $target = Build-TargetFilePath -Folder $exportDir -Id $id -Name $name -Code $code
  Copy-Item -LiteralPath $source -Destination $target -Force
  $copied += 1
  Write-Log "Copied id=$id -> $target"
}

Write-Log "Done. copied=$copied, missing=$missing, total=$($rows.Count)"

if ($copied -gt 0) {
  Start-Process explorer.exe $exportDir | Out-Null
}
