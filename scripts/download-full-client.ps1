param(
  [string]$DestinationPath
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir

if ([string]::IsNullOrWhiteSpace($DestinationPath)) {
  $DestinationPath = Join-Path $projectRoot "Duix.Avatar-1.0.6-setup.exe"
}

$url = 'https://github.com/duixcom/Duix-Avatar/releases/download/v1.0.6/Duix.Avatar-1.0.6-setup.exe'
Write-Host "Downloading Duix.Avatar 1.0.6 installer..."
Write-Host "URL:    $url"
Write-Host "Target: $DestinationPath"

if (Test-Path $DestinationPath) {
  Write-Host "Target file already exists. Checking size..."
  $existing = Get-Item $DestinationPath
  if ($existing.Length -gt 200000000) {
    Write-Host "Installer is already fully downloaded: $($existing.FullName) ($($existing.Length) bytes)"
    return
  }
  Remove-Item $DestinationPath -Force
}

$head = Invoke-WebRequest -Uri $url -Method Head -MaximumRedirection 5
$length = [int64]$head.Headers['Content-Length']
$chunkSize = 8MB

$fs = [System.IO.File]::Open(
  $DestinationPath,
  [System.IO.FileMode]::Create,
  [System.IO.FileAccess]::Write,
  [System.IO.FileShare]::Read
)

try {
  for ($start = 0; $start -lt $length; $start += $chunkSize) {
    $end = [Math]::Min($start + $chunkSize - 1, $length - 1)
    $success = $false

    for ($attempt = 1; $attempt -le 5 -and -not $success; $attempt++) {
      try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Method = 'GET'
        $req.AddRange($start, $end)
        $req.Timeout = 300000

        $resp = $req.GetResponse()
        $stream = $resp.GetResponseStream()
        $buffer = New-Object byte[] 65536

        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
          $fs.Write($buffer, 0, $read)
        }

        $stream.Close()
        $resp.Close()
        $success = $true
        $pct = [Math]::Round(($end / $length) * 100, 1)
        Write-Host "Downloaded: $pct% ($start - $end / $length bytes)"
      } catch {
        if ($attempt -eq 5) {
          throw
        }
        Write-Warning "Chunk download failed (attempt $attempt/5), retrying in 2s..."
        Start-Sleep -Seconds 2
      }
    }
  }
} finally {
  $fs.Close()
}

$finalItem = Get-Item $DestinationPath
Write-Host "Download complete: $($finalItem.FullName) ($($finalItem.Length) bytes)"
