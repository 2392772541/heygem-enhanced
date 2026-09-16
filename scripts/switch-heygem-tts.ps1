param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('index-tts2', 'fish-speech')]
  [string]$Mode,
  [switch]$Restart
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$modeFile = if ($env:HEYGEM_MODE_FILE) { $env:HEYGEM_MODE_FILE } else { Join-Path $projectRoot 'tts-mode.txt' }

Set-Content -Path $modeFile -Value $Mode -Encoding ASCII
Write-Host ("TTS mode set to: {0}" -f $Mode)

if ($Restart) {
  Write-Host "Restarting HeyGem stack in $Mode mode..."
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir 'stop-heygem.ps1')
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir 'start-heygem.ps1')
  exit $LASTEXITCODE
}
